"""Signature drift detection across workspace callers when a file is edited.

Compares pre-edit vs. post-edit symbols of an edited module and inspects the
workspace's call/import tables to catch broken external callers immediately.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .parsers import ParsedSymbol, python_call_compatibility


@dataclass(slots=True)
class DriftAlert:
    edited_path: str
    symbol_name: str
    severity: str  # "critical" | "warning"
    old_signature: str
    new_signature: str
    reason: str
    affected_callers: list[dict[str, Any]] = field(default_factory=list)
    baseline_signature: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "edited_path": self.edited_path,
            "symbol_name": self.symbol_name,
            "severity": self.severity,
            "old_signature": self.old_signature,
            "new_signature": self.new_signature,
            "reason": self.reason,
            "affected_callers": self.affected_callers,
        }


def _external_callers_for_symbol(
    conn: sqlite3.Connection,
    workspace_id: str,
    edited_path: str,
    symbol_name: str,
    *,
    include_imports: bool = True,
) -> list[dict[str, Any]]:
    """Find external call sites and explicit importers referencing *symbol_name*."""
    rows = conn.execute(
        """
        SELECT c.caller_path, c.caller_symbol, c.line_no, c.arg_count,
               c.python_call_json, c.resolved_path,
               EXISTS (
                   SELECT 1 FROM imports i
                   WHERE i.workspace_id = c.workspace_id
                     AND i.src_path = c.caller_path
                     AND i.resolved_path = c.resolved_path
                     AND instr(',' || i.imported_names || ',', ',' || c.callee_name || ',') > 0
               ) AS explicit_import
        FROM calls c
        WHERE c.workspace_id = ?
          AND c.callee_name = ?
          AND c.caller_path != ?
          AND (c.resolved_path = ? OR c.resolved_path = '')
        ORDER BY caller_path, line_no
        """,
        (workspace_id, symbol_name, edited_path, edited_path),
    ).fetchall()

    callers: list[dict[str, Any]] = []
    for row in rows:
        shape = _load_shape(row["python_call_json"])
        precise = shape is not None and shape.get("binding_version") == 2
        if precise and (row["resolved_path"] != edited_path or not shape.get("direct_import")):
            continue
        callers.append({
            "path": row["caller_path"], "caller": row["caller_symbol"],
            "line": int(row["line_no"]), "arg_count": int(row["arg_count"]), "kind": "call",
            "_python_call": shape if (row["resolved_path"] == edited_path
                and (precise or row["explicit_import"])) else None,
        })

    if include_imports:
        imp_rows = conn.execute(
            """
            SELECT src_path, imported_names, line_no
            FROM imports
            WHERE workspace_id = ?
              AND resolved_path = ?
              AND src_path != ?
            ORDER BY src_path, line_no
            """,
            (workspace_id, edited_path, edited_path),
        ).fetchall()
        for r in imp_rows:
            names = [n.strip() for n in (r["imported_names"] or "").split(",") if n.strip()]
            if symbol_name in names:
                callers.append(
                    {
                        "path": r["src_path"],
                        "caller": "<import>",
                        "line": int(r["line_no"]),
                        "arg_count": -1,
                        "kind": "import",
                    }
                )
    return callers


def _accepts_arg_count(min_args: int, max_args: int, count: int) -> bool:
    return count >= min_args and (max_args < 0 or count <= max_args)


def _load_shape(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        shape = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return shape if isinstance(shape, dict) else None


def _public_caller(caller: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in caller.items() if key != "_python_call"}
    shape = caller.get("_python_call")
    reference = _binding_reference(shape)
    if reference is not None:
        # Persist the proven import identity so later loss of static resolution
        # is distinguishable from a caller deliberately changing its target.
        result["binding_reference"] = reference
    if shape and shape.get("callee_expression"):
        result["callee_expression"] = shape["callee_expression"]
    return result


def _binding_reference(shape: dict[str, Any] | None) -> dict[str, Any] | None:
    if not shape or shape.get("binding_version") != 2 or not shape.get("direct_import"):
        return None
    return {key: shape.get(key) for key in ("import_module", "import_name", "import_kind", "attribute_path")}


def _previously_tracked(caller: dict[str, Any], tracked: list[dict[str, Any]]) -> bool:
    shape = caller.get("_python_call")
    reference = _binding_reference(shape)
    expression = shape.get("callee_expression") if shape else None
    for old in tracked:
        if (old.get("path"), old.get("caller")) != (caller.get("path"), caller.get("caller")):
            continue
        if reference is not None and old.get("binding_reference") is not None:
            if reference == old["binding_reference"]:
                return True
        elif expression and old.get("callee_expression"):
            if expression == old["callee_expression"]:
                return True
        elif _caller_location(old) == _caller_location(caller):
            return True
    return False


def _unresolved_tracked_callers(
    conn: sqlite3.Connection, workspace_id: str, tracked: list[dict[str, Any]], symbol_name: str,
) -> list[dict[str, Any]]:
    """Retain established risks when the same import loses target confidence.

    Only an already affected binding qualifies. New unknown receivers still do
    not create alerts, and removal or a changed import target closes the old risk.
    Match the import identity within its lexical caller even if lines shifted.
    If caller-side analysis became uncertain, retain the same source expression;
    its loss of a proven binding is not evidence that the old risk was repaired.
    """
    by_path: dict[str, list[dict[str, Any]]] = {}
    for caller in tracked:
        if caller.get("kind") == "call":
            by_path.setdefault(caller["path"], []).append(caller)
    unresolved = []
    for path, previous in by_path.items():
        rows = conn.execute(
            "SELECT caller_symbol, callee_name, line_no, arg_count, python_call_json FROM calls "
            "WHERE workspace_id = ? AND caller_path = ? AND resolved_path = ''",
            (workspace_id, path),
        ).fetchall()
        for row in rows:
            shape = _load_shape(row["python_call_json"])
            reference = _binding_reference(shape)
            if not shape or shape.get("binding_version") != 2:
                continue
            expression = shape.get("callee_expression")
            def same_reference(old: dict[str, Any]) -> bool:
                if old.get("caller") != row["caller_symbol"]:
                    return False
                if reference is not None and old.get("binding_reference") is not None:
                    return old["binding_reference"] == reference
                if reference is None and old.get("callee_expression") and expression:
                    return old["callee_expression"] == expression
                # Older persisted alerts lack a source expression. A surviving
                # same-named unresolved call remains uncertain after migration.
                return row["callee_name"] == symbol_name
            matched = next((old for old in previous if same_reference(old)), None)
            if matched is not None:
                unresolved.append({
                    "path": path, "caller": row["caller_symbol"], "line": int(row["line_no"]),
                    "arg_count": int(row["arg_count"]), "kind": "call",
                    "binding_reference": reference or matched.get("binding_reference"),
                    "callee_expression": expression or matched.get("callee_expression"),
                    "analysis_status": "needs_verification",
                    "incompatibility": "Previously affected call no longer resolves with static confidence; verify this caller.",
                })
    return unresolved


def _read_callers(raw: str) -> list[dict[str, Any]]:
    try:
        callers = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [caller for caller in callers if isinstance(caller, dict)] if isinstance(callers, list) else []


def _caller_location(caller: dict[str, Any]) -> tuple[str, str, int]:
    return caller.get("path", ""), caller.get("caller", ""), caller.get("line", 0)


def _python_arity(signature: dict[str, Any]) -> tuple[int, int]:
    maximum = -1 if signature["has_var_positional"] or signature["has_var_keyword"] else sum(
        len(signature[kind]) for kind in ("posonly", "positional_or_keyword", "keyword_only")
    )
    return len(signature["required"]), maximum


def _arity_narrowed(before: tuple[int, int], after: tuple[int, int]) -> bool:
    old_min, old_max = before
    new_min, new_max = after
    return new_min > old_min or (new_max >= 0 and (old_max < 0 or new_max < old_max))


def _python_contract_may_narrow(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Recognize simple compatible changes before warning about unpacked calls."""
    if before == after:
        return False
    if any(before[key] and not after[key] for key in ("has_var_positional", "has_var_keyword")):
        return True

    old_posonly, new_posonly = before["posonly"], after["posonly"]
    old_regular, new_regular = before["positional_or_keyword"], after["positional_or_keyword"]
    old_pos, new_pos = old_posonly + old_regular, new_posonly + new_regular
    old_required, new_required = set(before["required"]), set(after["required"])
    if len(old_pos) > len(new_pos) or not set(before["keyword_only"]).issubset(after["keyword_only"]):
        return True
    if (new_required & set(after["keyword_only"])) - (old_required & set(before["keyword_only"])):
        return True
    for index, name in enumerate(new_pos):
        if index >= len(old_pos):
            if name in new_required or (
                index >= len(new_posonly) and before["has_var_positional"] and before["has_var_keyword"]
            ):
                return True
            continue
        if name in new_required and old_pos[index] not in old_required:
            return True
        if index >= len(old_posonly) and (index < len(new_posonly) or name != old_pos[index]):
            return True
        if index < len(old_posonly) and index >= len(new_posonly) and before["has_var_keyword"]:
            return True
    return False


def _single_keyword_rename(before: dict[str, Any], after: dict[str, Any]) -> tuple[str, str] | None:
    if any(before[key] != after[key] for key in ("posonly", "has_var_positional", "has_var_keyword")):
        return None
    changes: list[tuple[str, str]] = []
    for kind in ("positional_or_keyword", "keyword_only"):
        if len(before[kind]) != len(after[kind]):
            return None
        changes.extend((old, new) for old, new in zip(before[kind], after[kind]) if old != new)
    if len(changes) != 1:
        return None
    old, new = changes[0]
    expected_required = {new if name == old else name for name in before["required"]}
    return (old, new) if expected_required == set(after["required"]) else None


def _classify_callers(
    callers: list[dict[str, Any]],
    old_shape: dict[str, Any] | None,
    new_shape: dict[str, Any] | None,
    old_arity: tuple[int, int] | None,
    new_arity: tuple[int, int],
    *,
    legacy_unknown_risk: bool,
    tracked_sites: frozenset[tuple[str, str, int]] = frozenset(),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate newly broken callers from ones requiring manual verification."""
    broken: list[dict[str, Any]] = []
    uncertain: list[dict[str, Any]] = []
    shape_narrowed = False
    rename = None
    if old_shape is not None and new_shape is not None:
        shape_narrowed = _python_contract_may_narrow(old_shape, new_shape)
        rename = _single_keyword_rename(old_shape, new_shape)
    for caller in callers:
        already_tracked = _caller_location(caller) in tracked_sites
        call_shape = caller.get("_python_call")
        if (old_shape is not None and new_shape is not None and call_shape is not None
                and call_shape.get("direct_import") is True):
            accepted_before, _ = python_call_compatibility(old_shape, call_shape)
            accepted_now, explanation = python_call_compatibility(new_shape, call_shape)
            if (accepted_before is True or already_tracked) and accepted_now is False:
                entry = {**_public_caller(caller), "incompatibility": explanation}
                if rename is not None:
                    old, new = rename
                    keywords = call_shape["keywords"]
                    if old in keywords and new not in keywords:
                        updated_call = {**call_shape, "keywords": [new if name == old else name for name in keywords]}
                        compatible, _ = python_call_compatibility(new_shape, updated_call)
                        if compatible is True:
                            entry["suggestion"] = f"Review replacing {old}= with {new}=."
                broken.append(entry)
            elif accepted_before is not False and accepted_now is None and shape_narrowed:
                uncertain.append(_public_caller(caller))
            continue

        if call_shape is not None and call_shape.get("binding_version") == 2 and new_shape is None:
            # A decorated/dynamic function has no proven static calling convention.
            continue
        count = caller["arg_count"]
        if count < 0:
            if legacy_unknown_risk:
                uncertain.append(_public_caller(caller))
        elif (already_tracked or old_arity is None or _accepts_arg_count(*old_arity, count)) and not _accepts_arg_count(*new_arity, count):
            broken.append(_public_caller(caller))
    return broken, uncertain


def _describe_break(
    symbol_name: str,
    affected: list[dict[str, Any]],
    arity: tuple[int, int],
) -> str:
    explanations = list(dict.fromkeys(c["incompatibility"] for c in affected if c.get("incompatibility")))
    if explanations:
        detail = ". ".join(text[0].upper() + text[1:] for text in explanations[:3])
        return (
            f"Signature of '{symbol_name}' no longer accepts {len(affected)} external call site(s). "
            f"{detail}."
        )
    minimum, maximum = arity
    return (
        f"Signature of '{symbol_name}' accepts {minimum}..{'*' if maximum < 0 else maximum} arg(s); "
        f"{len(affected)} external call site(s) pass incompatible argument counts."
    )


def _record_alert(
    conn: sqlite3.Connection, workspace_id: str, alert: DriftAlert
) -> None:
    """Update an unresolved contract break without losing its original baseline."""
    existing = conn.execute(
        """
        SELECT id, old_signature, affected_callers_json FROM drift_alerts
        WHERE workspace_id = ? AND edited_path = ? AND symbol_name = ? AND resolved = 0
        ORDER BY id DESC LIMIT 1
        """,
        (workspace_id, alert.edited_path, alert.symbol_name),
    ).fetchone()
    if existing is not None:
        alert.old_signature = existing["old_signature"]
        # Keep known sites available for reconciliation if a caller was edited
        # into a different invalid call before the definition changed again.
        sites = {_caller_location(c): c for c in _read_callers(existing["affected_callers_json"])}
        sites.update({_caller_location(c): c for c in alert.affected_callers})
        conn.execute(
            """
            UPDATE drift_alerts
            SET severity = ?, new_signature = ?, reason = ?, affected_callers_json = ?
            WHERE id = ?
            """,
            (
                alert.severity, alert.new_signature, alert.reason,
                json.dumps(list(sites.values())), int(existing["id"]),
            ),
        )
        return
    conn.execute(
        """
        INSERT INTO drift_alerts (
            workspace_id, created_at, edited_path, symbol_name, severity,
            old_signature, new_signature, reason, affected_callers_json, resolved,
            baseline_signature_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            workspace_id, time.time(), alert.edited_path, alert.symbol_name,
            alert.severity, alert.old_signature, alert.new_signature, alert.reason,
            json.dumps(alert.affected_callers),
            json.dumps(alert.baseline_signature) if alert.baseline_signature is not None else None,
        ),
    )


def reconcile_drift_alerts(
    conn: sqlite3.Connection, workspace_id: str
) -> list[DriftAlert]:
    """Refresh open alerts against the current graph; resolve only repaired contracts.

    Run after storing edited files and relinking callers. Unknown call arity is
    retained as a warning, not interpreted as proof that a caller was repaired.
    """
    rows = conn.execute(
        "SELECT * FROM drift_alerts WHERE workspace_id = ? AND resolved = 0 ORDER BY id",
        (workspace_id,),
    ).fetchall()
    status_row = conn.execute("SELECT scan_status_json FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
    try:
        status = json.loads(status_row[0]) if status_row else {}
    except (ValueError, TypeError):
        status = {}
    unavailable = {entry["path"] for entry in [*status.get("stale_files", []), *status.get("parse_errors", [])]}
    if unavailable:
        # A true deletion has no retained row, even if a previous scan warning
        # has not yet been replaced by the transaction's final summary.
        indexed = {r[0] for r in conn.execute("SELECT rel_path FROM files WHERE workspace_id = ?", (workspace_id,))}
        unavailable.intersection_update(indexed)
    active: list[DriftAlert] = []
    for row in rows:
        tracked = _read_callers(row["affected_callers_json"])
        symbol = conn.execute(
            """
            SELECT signature, min_args, max_args, python_signature_json FROM symbols
            WHERE workspace_id = ? AND rel_path = ? AND qualified_name = ?
            """,
            (workspace_id, row["edited_path"], row["symbol_name"]),
        ).fetchone()
        callers = _external_callers_for_symbol(
            conn, workspace_id, row["edited_path"], row["symbol_name"].split(".")[-1],
            include_imports=symbol is None,
        )
        stale_callers = []
        fresh_callers = []
        for caller in callers:
            if caller["path"] in unavailable or row["edited_path"] in unavailable:
                if _previously_tracked(caller, tracked):
                    stale_callers.append({
                        **_public_caller(caller), "analysis_status": "needs_verification",
                        "incompatibility": "Caller or target source was not reindexed; verify against current files.",
                    })
            else:
                fresh_callers.append(caller)
        callers = fresh_callers
        severity = "critical"
        new_signature = "<removed>"
        baseline = _load_shape(row["baseline_signature_json"])
        if symbol is None:
            affected = [_public_caller(caller) for caller in callers]
            reason = (
                f"Exported symbol '{row['symbol_name']}' was removed, "
                f"but {len(affected)} external site(s) still reference it."
            )
        else:
            new_signature = symbol["signature"]
            current = _load_shape(symbol["python_signature_json"])
            old_arity = _python_arity(baseline) if baseline is not None else None
            new_arity = (int(symbol["min_args"]), int(symbol["max_args"]))
            affected, uncertain = _classify_callers(
                callers, baseline, current, old_arity, new_arity,
                legacy_unknown_risk=(
                    _arity_narrowed(old_arity, new_arity) if old_arity is not None
                    else new_signature != row["old_signature"]
                ),
                tracked_sites=frozenset(
                    _caller_location(caller) for caller in tracked
                    if caller.get("kind") == "call"
                ),
            )
            if affected:
                reason = _describe_break(row["symbol_name"], affected, new_arity)
            else:
                affected = uncertain
                severity = "warning"
                reason = (
                    f"The signature of '{row['symbol_name']}' may reject "
                    f"{len(affected)} external call site(s) with unpacked or unknown arguments."
                )

        unresolved = stale_callers + _unresolved_tracked_callers(
            conn, workspace_id, tracked, row["symbol_name"].split(".")[-1],
        )
        if unresolved:
            if not affected:
                severity = "warning"
                reason = (
                    f"{len(unresolved)} previously affected external call site(s) need verification: "
                    "their source or imported target could not be verified against current files."
                )
            else:
                reason += f" {len(unresolved)} additional previously affected site(s) need verification after loss of static target confidence."
            affected.extend(unresolved)

        if not affected:
            conn.execute("UPDATE drift_alerts SET resolved = 1 WHERE id = ?", (int(row["id"]),))
            continue
        alert = DriftAlert(
            edited_path=row["edited_path"], symbol_name=row["symbol_name"],
            severity=severity, old_signature=row["old_signature"],
            new_signature=new_signature, reason=reason, affected_callers=affected,
        )
        conn.execute(
            """
            UPDATE drift_alerts
            SET severity = ?, new_signature = ?, reason = ?, affected_callers_json = ?
            WHERE id = ?
            """,
            (severity, new_signature, reason, json.dumps(alert.affected_callers), int(row["id"])),
        )
        active.append(alert)
    return active


def detect_and_record_drift(
    conn: sqlite3.Connection,
    workspace_id: str,
    edited_path: str,
    old_symbols: dict[str, ParsedSymbol],
    new_symbols: dict[str, ParsedSymbol],
) -> list[DriftAlert]:
    """Compare old vs. new symbol definitions for *edited_path* and persist open drift alerts."""
    alerts: list[DriftAlert] = []

    # 1. Deleted exported symbols that external files still call or import
    for qname, old_sym in old_symbols.items():
        if not old_sym.is_exported or qname in new_symbols:
            continue
        callers = _external_callers_for_symbol(conn, workspace_id, edited_path, old_sym.name)
        if callers:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=old_sym.qualified_name,
                    severity="critical",
                    old_signature=old_sym.signature,
                    new_signature="<removed>",
                    reason=(
                        f"Exported symbol '{old_sym.qualified_name}' was removed, "
                        f"but {len(callers)} external site(s) still reference it."
                    ),
                    affected_callers=[_public_caller(caller) for caller in callers],
                    baseline_signature=old_sym.python_signature,
                )
            )

    # 2. Modified signatures where arity or parameter contract breaks external callers
    for qname, new_sym in new_symbols.items():
        old_sym = old_symbols.get(qname)
        if old_sym is None:
            continue
        old_arity = (old_sym.min_args, old_sym.max_args)
        new_arity = (new_sym.min_args, new_sym.max_args)
        old_shape = old_sym.python_signature
        new_shape = new_sym.python_signature
        if old_arity == new_arity and old_shape == new_shape:
            continue

        callers = _external_callers_for_symbol(
            conn, workspace_id, edited_path, new_sym.name, include_imports=False
        )
        if not callers:
            continue

        broken, uncertain = _classify_callers(
            callers, old_shape, new_shape, old_arity, new_arity,
            legacy_unknown_risk=_arity_narrowed(old_arity, new_arity),
        )

        if broken:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=new_sym.qualified_name,
                    severity="critical",
                    old_signature=old_sym.signature,
                    new_signature=new_sym.signature,
                    reason=_describe_break(new_sym.qualified_name, broken, new_arity),
                    affected_callers=broken,
                    baseline_signature=old_shape,
                )
            )
        elif uncertain:
            alerts.append(
                DriftAlert(
                    edited_path=edited_path,
                    symbol_name=new_sym.qualified_name,
                    severity="warning",
                    old_signature=old_sym.signature,
                    new_signature=new_sym.signature,
                    reason=(
                        f"The signature of '{new_sym.qualified_name}' may reject "
                        f"{len(uncertain)} external call site(s) with unpacked or unknown arguments."
                    ),
                    affected_callers=uncertain,
                    baseline_signature=old_shape,
                )
            )

    for a in alerts:
        _record_alert(conn, workspace_id, a)
    return alerts


def list_open_drift_alerts(
    conn: sqlite3.Connection,
    workspace_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, created_at, edited_path, symbol_name, severity,
               old_signature, new_signature, reason, affected_callers_json
        FROM drift_alerts
        WHERE workspace_id = ? AND resolved = 0
        ORDER BY id DESC
        LIMIT ?
        """,
        (workspace_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        callers = _read_callers(r["affected_callers_json"])
        out.append(
            {
                "id": int(r["id"]),
                "created_at": float(r["created_at"]),
                "edited_path": r["edited_path"],
                "symbol_name": r["symbol_name"],
                "severity": r["severity"],
                "old_signature": r["old_signature"],
                "new_signature": r["new_signature"],
                "reason": r["reason"],
                "affected_callers": callers,
            }
        )
    return out
