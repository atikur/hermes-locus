"""Rehearse a signature change before any file is edited.

Every statically proven external caller of a symbol is bound against a
*proposed* signature with the same rules the post-edit drift guard applies.
Nothing is written. The result is a prediction and, for the one mechanical
case of a single renamed keyword, a caller patch to review and apply.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
from pathlib import Path
from typing import Any, Optional

from .drift import (
    _external_callers_for_symbol,
    _load_shape,
    _python_contract_may_narrow,
    _single_keyword_rename,
)
from .graph import _ambiguous_target, _ensure_indexed, _scan_status, _symbol_choices
from .indexer import DEFAULT_MAX_FILE_BYTES
from .parsers import (
    PythonSignature,
    _attribute_parts,
    _python_signature,
    _without_receiver,
    python_call_compatibility,
)
from .storage import open_locus_db

MAX_PROPOSAL_CHARS = 2000
MAX_LISTED_CALLERS = 40
MAX_PATCH_FILES = 20
MAX_PATCH_CHARS = 24_000


def split_rehearsal_request(text: str) -> tuple[str, str]:
    """Split ``target(params)`` into the symbol target and its parameter list.

    Accepts ``def name(...) -> T:`` as written in source, ``path::Class.method(...)``,
    or a bare ``(...)`` when the target is given separately.
    """
    clean = (text or "").strip()
    if clean.startswith("async "):
        clean = clean[6:].lstrip()
    if clean.startswith("def "):
        clean = clean[4:].lstrip()
    start, end = clean.find("("), clean.rfind(")")
    if start < 0 or end < start:
        return clean, ""
    return clean[:start].strip(), clean[start:end + 1]


def parse_proposed_parameters(params: str) -> tuple[PythonSignature, str]:
    """Read a parenthesised parameter list; raise ``ValueError`` when it is not valid Python."""
    clean = (params or "").strip()
    if not clean.startswith("(") or not clean.endswith(")"):
        raise ValueError("Give the proposed parameters in parentheses, for example `(token, *, scope=None)`.")
    if len(clean) > MAX_PROPOSAL_CHARS:
        raise ValueError(f"The proposed parameter list exceeds {MAX_PROPOSAL_CHARS} characters.")
    try:
        tree = ast.parse(f"def _locus_rehearsal{clean}:\n    pass\n")
    except SyntaxError as exc:
        raise ValueError(f"The proposed parameter list is not valid Python: {exc.msg}.") from None
    node = tree.body[0] if len(tree.body) == 1 else None
    if not isinstance(node, ast.FunctionDef):
        raise ValueError("The proposal must be a single parameter list.")
    return _python_signature(node.args), f"({ast.unparse(node.args)})"


def _unproven_reason(symbol: dict[str, Any]) -> str:
    kind = symbol.get("kind")
    if kind == "method":
        return ("it is decorated, redefined, or defined on a class Locus cannot prove "
                "(class decorator, metaclass, `__new__`, or imported base)")
    if kind == "class":
        return ("its constructor is not statically provable (decorator such as a dataclass, metaclass, "
                "`__new__`, or an imported base class)")
    if kind in ("function", "async_function"):
        return "a decorator may replace its declared parameters"
    return f"a {kind} has no call signature that Locus checks"


def _caller_patch(root: Path, conn: Any, ws_id: str, renames: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Build a reviewable patch that renames one keyword at each proven breaking call site.

    Every entry carries ``rename: [old, new]``. Each file must still match the
    indexed content, so positions come from the same source the analysis used.
    Locus never applies the patch itself.
    """
    by_path: dict[str, list[dict[str, Any]]] = {}
    for caller in renames:
        by_path.setdefault(caller["path"], []).append(caller)
    sections: list[str] = []
    skipped: list[dict[str, str]] = []
    files: list[str] = []
    calls = 0
    for path in sorted(by_path):
        if len(files) >= MAX_PATCH_FILES:
            skipped.append({"path": path, "reason": f"patch limited to {MAX_PATCH_FILES} files"})
            continue
        target = root / path
        row = conn.execute(
            "SELECT sha256 FROM files WHERE workspace_id = ? AND rel_path = ?", (ws_id, path)
        ).fetchone()
        try:
            if target.is_symlink() or not target.is_file() or target.stat().st_size > DEFAULT_MAX_FILE_BYTES:
                raise OSError("unreadable")
            target.resolve().relative_to(root)
            raw = target.read_bytes()
        except (OSError, ValueError):
            skipped.append({"path": path, "reason": "source could not be read"})
            continue
        if row is None or hashlib.sha256(raw).hexdigest() != row["sha256"]:
            skipped.append({"path": path, "reason": "changed since it was indexed; reindex and rehearse again"})
            continue
        try:
            before = raw.decode("utf-8")
            tree = ast.parse(before)
        except (UnicodeDecodeError, SyntaxError):
            skipped.append({"path": path, "reason": "source could not be parsed"})
            continue
        wanted = {(c["line"], tuple(c.get("callee_expression") or ())): tuple(c["rename"]) for c in by_path[path]}
        # (line number, UTF-8 column, old name, new name) of each keyword to rename
        edits: list[tuple[int, int, str, str]] = []
        found_calls = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                rename = wanted.get((node.lineno, tuple(_attribute_parts(node.func))))
                found = [kw for kw in node.keywords if rename and kw.arg == rename[0]]
                edits.extend((kw.lineno, kw.col_offset, *rename) for kw in found)
                found_calls += bool(found)
        lines = before.split("\n")
        applied = True
        for line_no, column, old, new in sorted(set(edits), reverse=True):
            encoded = lines[line_no - 1].encode("utf-8")
            if encoded[column:column + len(old.encode())] != old.encode():
                applied = False
                break
            lines[line_no - 1] = (encoded[:column] + new.encode() + encoded[column + len(old.encode()):]).decode("utf-8")
        after = "\n".join(lines)
        try:
            ast.parse(after)
        except SyntaxError:
            applied = False
        if not applied or not edits or after == before:
            skipped.append({"path": path, "reason": "call site could not be located exactly"})
            continue
        diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))[2:]
        body = "\n".join("@@" if line.startswith("@@ ") else line for line in diff)
        sections.append(f"*** Update File: {path}\n{body}")
        files.append(path)
        calls += found_calls
    if not sections:
        return {"patch": "", "files": [], "calls": 0, "skipped": skipped} if skipped else None
    patch = "*** Begin Patch\n" + "\n".join(sections) + "\n*** End Patch"
    if len(patch) > MAX_PATCH_CHARS:
        return {"patch": "", "files": [], "calls": 0,
                "skipped": [{"path": "*", "reason": f"patch exceeds {MAX_PATCH_CHARS} characters; rename the callers file by file"}]}
    return {"patch": patch, "files": files, "calls": calls, "skipped": skipped}


def rehearse_signature_change(
    root_path: str | Path,
    target: str,
    proposed_parameters: str,
    *,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Predict which statically proven callers a proposed signature would break."""
    target = (target or "").strip()
    if not target:
        return {"ok": False, "error": "Name the symbol to rehearse, for example `verify_token(token, *, scope=None)`."}
    try:
        proposed, rendered = parse_proposed_parameters(proposed_parameters)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    matches = _symbol_choices(root, target, db_path)
    if not matches:
        return {"ok": False, "error": f"No symbol matching '{target}' found in workspace."}
    if len(matches) > 1 or "python_signature_json" not in matches[0]:
        return _ambiguous_target(target, [m["symbol_id"] for m in matches])
    symbol = matches[0]

    with open_locus_db(db_path=db_path) as conn:
        if symbol["qualified_name"].endswith(".__init__"):
            # Calling the class is the contract a changed __init__ must satisfy.
            owner = conn.execute(
                "SELECT * FROM symbols WHERE workspace_id = ? AND rel_path = ? AND qualified_name = ?",
                (ws_id, symbol["rel_path"], symbol["qualified_name"][:-len(".__init__")]),
            ).fetchone()
            if owner is not None:
                symbol = dict(owner)
        display = symbol["signature"]
        if symbol["kind"] == "class":
            init = conn.execute(
                "SELECT signature FROM symbols WHERE workspace_id = ? AND rel_path = ? AND qualified_name = ?",
                (ws_id, symbol["rel_path"], symbol["qualified_name"] + ".__init__"),
            ).fetchone()
            if init is not None:
                display = init["signature"]
        public_symbol = {key: symbol[key] for key in ("symbol_id", "name", "qualified_name", "kind", "rel_path", "line_start")}
        scan_status = _scan_status(conn, ws_id)
        current = _load_shape(symbol.get("python_signature_json"))
        if current is None:
            return {"ok": False, "symbol": public_symbol, "scan_status": scan_status,
                    "error": f"`{symbol['qualified_name']}` has no statically proven calling convention: "
                             f"{_unproven_reason(symbol)}. A rehearsal would be a guess."}

        # The stored contract omits an implicit receiver; accept a proposal written as in source.
        first = (proposed["posonly"] or proposed["positional_or_keyword"] or [""])[0]
        current_first = (current["posonly"] or current["positional_or_keyword"] or [""])[0]
        if first in {"self", "cls"} and first != current_first and symbol["kind"] in {"class", "method"}:
            proposed = _without_receiver(proposed)

        callers = _external_callers_for_symbol(
            conn, ws_id, symbol["rel_path"], symbol["name"], include_imports=False,
            qualname=symbol["qualified_name"],
        )
        rename = _single_keyword_rename(current, proposed)
        narrows = _python_contract_may_narrow(current, proposed)
        breaks: list[dict[str, Any]] = []
        review: list[dict[str, Any]] = []
        compatible: list[dict[str, Any]] = []
        for caller in callers:
            shape = caller.get("_python_call")
            entry = {"path": caller["path"], "caller": caller["caller"], "line": caller["line"],
                     "callee_expression": (shape or {}).get("callee_expression")}
            if shape is None:
                review.append({**entry, "reason": "call shape is unavailable"})
                continue
            accepted_before, _ = python_call_compatibility(current, shape)
            accepted_after, explanation = python_call_compatibility(proposed, shape)
            if accepted_after is False:
                entry["incompatibility"] = explanation
                if accepted_before is False:
                    entry["already_incompatible"] = True
                if rename is not None:
                    old, new = rename
                    keywords = shape["keywords"]
                    if old in keywords and new not in keywords:
                        updated = {**shape, "keywords": [new if name == old else name for name in keywords]}
                        if python_call_compatibility(proposed, updated)[0] is True:
                            entry["rename"] = [old, new]
                            entry["suggestion"] = f"Replace {old}= with {new}=."
                breaks.append(entry)
            elif accepted_after is None and narrows:
                review.append({**entry, "reason": explanation})
            else:
                compatible.append(entry)

        unchecked = {
            "same_file": int(conn.execute(
                "SELECT COUNT(*) FROM calls WHERE workspace_id = ? AND caller_path = ? AND callee_name = ? "
                "AND resolved_path = ''",
                (ws_id, symbol["rel_path"], symbol["name"]),
            ).fetchone()[0]),
            "unresolved_same_name": int(conn.execute(
                "SELECT COUNT(*) FROM calls WHERE workspace_id = ? AND caller_path != ? AND callee_name = ? "
                "AND resolved_path = ''",
                (ws_id, symbol["rel_path"], symbol["name"]),
            ).fetchone()[0]),
        }
        caller_patch = None
        mechanical = [entry for entry in breaks if entry.get("rename")]
        if mechanical:
            caller_patch = _caller_patch(root, conn, ws_id, mechanical)

    return {
        "ok": True,
        "symbol": public_symbol,
        "current_signature": display,
        "proposed_parameters": rendered,
        "changes_contract": current != proposed,
        "keyword_rename": list(rename) if rename else None,
        "checked_call_sites": len(callers),
        "breaks": breaks,
        "needs_review": review,
        "compatible_count": len(compatible),
        "compatible": compatible[:MAX_LISTED_CALLERS],
        "unchecked": unchecked,
        "caller_patch": caller_patch,
        "scan_status": scan_status,
    }


def repair_patch_for_alerts(
    root_path: str | Path, alerts: list[dict[str, Any]], *, db_path: Optional[str | Path] = None,
) -> Optional[dict[str, Any]]:
    """After an edit, build one caller patch for the open alerts' mechanical keyword renames."""
    renames: dict[tuple[str, int, tuple[str, ...]], dict[str, Any]] = {}
    for alert in alerts:
        for caller in alert.get("affected_callers", []):
            if caller.get("rename") and caller.get("kind") == "call" and caller.get("callee_expression"):
                key = (caller["path"], caller["line"], tuple(caller["callee_expression"]))
                if key in renames and renames[key]["rename"] != caller["rename"]:
                    return None  # two different renames at one call: leave it to review
                renames[key] = caller
    if not renames:
        return None
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    with open_locus_db(db_path=db_path) as conn:
        return _caller_patch(root, conn, ws_id, list(renames.values()))


def rehearsal_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Compact machine-readable fields for a tool result."""
    patch = data.get("caller_patch") or {}
    return {
        "checked_call_sites": data.get("checked_call_sites", 0),
        "would_break": [f"{c['path']}:{c['line']}" for c in data.get("breaks", [])],
        "needs_review": [f"{c['path']}:{c['line']}" for c in data.get("needs_review", [])],
        "compatible_count": data.get("compatible_count", 0),
        "unchecked": data.get("unchecked", {}),
        "caller_patch": patch.get("patch") or None,
        "caller_patch_files": patch.get("files", []),
    }


__all__ = [
    "parse_proposed_parameters",
    "rehearsal_summary",
    "rehearse_signature_change",
    "repair_patch_for_alerts",
    "split_rehearsal_request",
]
