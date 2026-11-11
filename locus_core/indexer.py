"""Incremental workspace scanner, cross-file import/call resolver, and PageRank updater."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from .drift import detect_and_record_drift, reconcile_drift_alerts
from .parsers import (
    LANGUAGE_BY_EXT,
    ParsedFile,
    ParsedSymbol,
    detect_language,
    parse_source_file,
)
from .storage import open_locus_db, workspace_id_for

SKIP_DIR_NAMES = frozenset(
    {
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        "dist",
        "build",
        "out",
        "target",
        "vendor",
        "coverage",
        "site-packages",
        "eggs",
    }
)

DEFAULT_MAX_FILES = 2500
DEFAULT_MAX_FILE_BYTES = 512 * 1024  # 512 KiB
MAX_ACTIVITY_LOG_ROWS = 200

_BLOCKED_SYSTEM_ROOTS: tuple[str, ...] = (
    "/etc",
    "/private/etc",
    "/System",
    "/usr",
    "/bin",
    "/sbin",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
)

_SENSITIVE_HOME_SUBTREES: tuple[tuple[str, ...], ...] = (
    (".ssh",),
    (".aws",),
    (".gnupg",),
    (".kube",),
    (".docker",),
    (".azure",),
    (".npm",),
    (".cargo", "credentials"),
    (".config", "gh"),
    (".config", "gcloud"),
    (".config", "op"),
    (".config", "stripe"),
    (".hermes", "profiles"),
    (".hermes", "sessions"),
    (".hermes", "auth"),
    (".hermes", "credentials"),
)


def validate_safe_workspace_root(root_path: str | Path) -> tuple[bool, str]:
    """Verify *root_path* is an existing project directory and not a sensitive credential or OS root."""
    try:
        raw = Path(root_path).expanduser()
        if raw.is_symlink():
            return False, f"Refusing to index symlinked workspace root: {raw}"
        root = raw.resolve()
    except OSError as exc:
        return False, f"Invalid workspace path: {exc}"

    if not root.is_dir():
        return False, f"Workspace directory does not exist: {root}"
    if root.parent == root:
        return False, "Refusing to index filesystem root."
    if root.name.startswith("."):
        return False, f"Refusing to index hidden dot-directory: {root.name}"

    root_posix = root.as_posix()
    for sys_prefix in _BLOCKED_SYSTEM_ROOTS:
        if root_posix == sys_prefix or root_posix.startswith(sys_prefix + "/"):
            return False, f"Refusing to index OS system directory: {root}"

    try:
        home = Path.home().resolve()
        if root == home:
            return False, "Refusing to index user home root directory."
        rel_parts = root.relative_to(home).parts
        for blocked in _SENSITIVE_HOME_SUBTREES:
            if len(rel_parts) >= len(blocked) and rel_parts[: len(blocked)] == blocked:
                return False, "Refusing to index sensitive credential or agent state directory."
    except (OSError, ValueError):
        pass

    return True, ""


def _prune_activity_log(conn: sqlite3.Connection, workspace_id: str) -> None:
    conn.execute(
        """
        DELETE FROM activity_log
        WHERE workspace_id = ?
          AND id NOT IN (
              SELECT id FROM activity_log
              WHERE workspace_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (workspace_id, workspace_id, MAX_ACTIVITY_LOG_ROWS),
    )


def _load_gitignore_patterns(root: Path) -> list[str]:
    gi = root / ".gitignore"
    patterns: list[str] = []
    try:
        if gi.is_file() and gi.stat().st_size < 65536:
            for raw in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or line.startswith("!"):
                    continue
                patterns.append(line.rstrip("/"))
    except OSError:
        pass
    return patterns


def _matches_gitignore(rel_posix: str, name: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if "/" in pat:
            clean = pat.lstrip("/")
            if fnmatch.fnmatch(rel_posix, clean) or rel_posix.startswith(clean + "/"):
                return True
        else:
            if fnmatch.fnmatch(name, pat):
                return True
    return False


def _iter_workspace_source_files(
    root: Path,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> list[tuple[Path, str, os.stat_result]]:
    """Walk *root* safely (no dot-files/dirs, no symlink escapes), returning source files."""
    resolved_root = root.resolve()
    gi_patterns = _load_gitignore_patterns(resolved_root)
    collected: list[tuple[Path, str, os.stat_result]] = []

    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        cur_dir = Path(dirpath)
        try:
            rel_dir = cur_dir.relative_to(resolved_root).as_posix()
        except ValueError:
            dirnames[:] = []
            continue
        if rel_dir == ".":
            rel_dir = ""

        # Prune dot-directories, build/vendor folders, symlinks, and gitignored dirs in-place
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            if d.startswith(".") or d in SKIP_DIR_NAMES:
                continue
            d_full = cur_dir / d
            if d_full.is_symlink():
                continue
            d_rel = f"{rel_dir}/{d}".lstrip("/")
            if _matches_gitignore(d_rel, d, gi_patterns):
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            if fname.startswith("."):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in LANGUAGE_BY_EXT:
                continue
            f_full = cur_dir / fname
            if f_full.is_symlink():
                continue
            rel_posix = f"{rel_dir}/{fname}".lstrip("/")
            if _matches_gitignore(rel_posix, fname, gi_patterns):
                continue
            try:
                st = f_full.stat()
                if st.st_size > max_file_bytes or st.st_size == 0:
                    continue
                # Containment check
                f_full.resolve().relative_to(resolved_root)
            except (OSError, ValueError):
                continue

            collected.append((f_full, rel_posix, st))
            if len(collected) >= max_files:
                return collected
    return collected


def _resolve_import_spec(
    src_path: str,
    lang: str,
    target_spec: str,
    known_files: set[str],
    imported_names: list[str],
) -> str:
    """Resolve an import specifier to an indexed workspace ``rel_path`` (or ``""`` if external)."""
    spec = target_spec.strip()
    if not spec:
        return ""

    src_dir = posixpath.dirname(src_path)

    if lang == "python":
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            remainder = spec.lstrip(".")
            anchor_parts = [p for p in src_dir.split("/") if p]
            up = max(0, level - 1)
            if up > len(anchor_parts):
                return ""
            base_parts = anchor_parts[: len(anchor_parts) - up]
            if remainder:
                base_parts.extend(remainder.split("."))
            base_rel = "/".join(base_parts)
            candidates = []
            if base_rel:
                candidates.extend([f"{base_rel}.py", f"{base_rel}.pyi", f"{base_rel}/__init__.py"])
            for imp_name in imported_names:
                sub = f"{base_rel}/{imp_name}" if base_rel else imp_name
                candidates.extend([f"{sub}.py", f"{sub}/__init__.py"])
            for c in candidates:
                if c in known_files and c != src_path:
                    return c
            return ""
        else:
            mod_path = spec.replace(".", "/")
            prefixes = ("", "src/", "lib/", "app/")
            for pfx in prefixes:
                for cand in (
                    f"{pfx}{mod_path}.py",
                    f"{pfx}{mod_path}.pyi",
                    f"{pfx}{mod_path}/__init__.py",
                ):
                    if cand in known_files and cand != src_path:
                        return cand
                for imp_name in imported_names:
                    sub_cand = f"{pfx}{mod_path}/{imp_name}.py"
                    if sub_cand in known_files and sub_cand != src_path:
                        return sub_cand
            return ""

    if lang in ("typescript", "javascript"):
        if spec.startswith("."):
            joined = posixpath.normpath(posixpath.join(src_dir or ".", spec))
            if joined.startswith(".."):
                return ""
        elif spec.startswith("@/") or spec.startswith("~/"):
            joined = posixpath.normpath(posixpath.join("src", spec[2:]))
        else:
            return ""
        exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
        if joined in known_files:
            return joined
        # Strip explicit .js extension used in TypeScript ESM imports
        stem, existing_ext = posixpath.splitext(joined)
        bases = [joined, stem] if existing_ext in (".js", ".mjs") else [joined]
        for b in bases:
            for ext in exts:
                if f"{b}{ext}" in known_files:
                    return f"{b}{ext}"
            for ext in exts:
                idx_cand = f"{b}/index{ext}"
                if idx_cand in known_files:
                    return idx_cand
        return ""

    if lang == "go":
        tail = spec.rstrip("/").split("/")[-1]
        for kf in known_files:
            if kf != src_path and kf.endswith(".go") and not kf.endswith("_test.go"):
                if posixpath.dirname(kf).split("/")[-1] == tail:
                    return kf
        return ""

    if lang == "rust":
        cleaned = spec.replace("crate::", "").replace("super::", "").split("::")[0].strip()
        if not cleaned:
            return ""
        for cand in (
            f"{src_dir}/{cleaned}.rs".lstrip("/"),
            f"src/{cleaned}.rs",
            f"src/{cleaned}/mod.rs",
        ):
            if cand in known_files and cand != src_path:
                return cand
    return ""


def _store_parsed_file(
    conn: sqlite3.Connection,
    workspace_id: str,
    parsed: ParsedFile,
    mtime: float,
    size_bytes: int,
    sha256: str,
) -> None:
    conn.execute(
        "DELETE FROM symbols WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, parsed.rel_path),
    )
    conn.execute(
        "DELETE FROM imports WHERE workspace_id = ? AND src_path = ?",
        (workspace_id, parsed.rel_path),
    )
    conn.execute(
        "DELETE FROM calls WHERE workspace_id = ? AND caller_path = ?",
        (workspace_id, parsed.rel_path),
    )

    conn.execute(
        """
        INSERT INTO files (
            workspace_id, rel_path, lang, mtime, size_bytes, sha256,
            line_count, is_test
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
            lang = excluded.lang,
            mtime = excluded.mtime,
            size_bytes = excluded.size_bytes,
            sha256 = excluded.sha256,
            line_count = excluded.line_count,
            is_test = excluded.is_test
        """,
        (
            workspace_id,
            parsed.rel_path,
            parsed.lang,
            mtime,
            size_bytes,
            sha256,
            parsed.line_count,
            1 if parsed.is_test else 0,
        ),
    )

    if parsed.symbols:
        conn.executemany(
            """
            INSERT OR REPLACE INTO symbols (
                workspace_id, symbol_id, name, qualified_name, kind, rel_path,
                line_start, line_end, signature, min_args, max_args, is_exported, docstring
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    workspace_id,
                    s.symbol_id,
                    s.name,
                    s.qualified_name,
                    s.kind,
                    s.rel_path,
                    s.line_start,
                    s.line_end,
                    s.signature,
                    s.min_args,
                    s.max_args,
                    1 if s.is_exported else 0,
                    s.docstring,
                )
                for s in parsed.symbols
            ],
        )

    if parsed.imports:
        conn.executemany(
            """
            INSERT INTO imports (workspace_id, src_path, target_spec, resolved_path, imported_names, line_no)
            VALUES (?, ?, ?, '', ?, ?)
            """,
            [
                (
                    workspace_id,
                    imp.src_path,
                    imp.target_spec,
                    ",".join(imp.imported_names),
                    imp.line_no,
                )
                for imp in parsed.imports
            ],
        )

    if parsed.calls:
        conn.executemany(
            """
            INSERT INTO calls (workspace_id, caller_path, caller_symbol, callee_name, resolved_path, line_no, arg_count)
            VALUES (?, ?, ?, ?, '', ?, ?)
            """,
            [
                (
                    workspace_id,
                    c.caller_path,
                    c.caller_symbol,
                    c.callee_name,
                    c.line_no,
                    c.arg_count,
                )
                for c in parsed.calls
            ],
        )


def _relink_and_rank(conn: sqlite3.Connection, workspace_id: str) -> int:
    """Resolve cross-file imports & calls and compute PageRank + fan-in/fan-out."""
    file_rows = conn.execute(
        "SELECT rel_path, lang FROM files WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    known_files = {r["rel_path"] for r in file_rows}
    lang_by_file = {r["rel_path"]: r["lang"] for r in file_rows}
    if not known_files:
        return 0

    # 1. Resolve imports
    imp_rows = conn.execute(
        "SELECT rowid, src_path, target_spec, imported_names FROM imports WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()

    imp_updates: list[tuple[str, int]] = []
    imported_map: dict[str, dict[str, str]] = {f: {} for f in known_files}
    file_imports: dict[str, set[str]] = {f: set() for f in known_files}

    for r in imp_rows:
        src = r["src_path"]
        lang = lang_by_file.get(src, "python")
        names = [n for n in (r["imported_names"] or "").split(",") if n]
        resolved = _resolve_import_spec(src, lang, r["target_spec"], known_files, names)
        imp_updates.append((resolved, int(r["rowid"])))
        if resolved:
            file_imports.setdefault(src, set()).add(resolved)
            for n in names:
                imported_map.setdefault(src, {})[n] = resolved

    if imp_updates:
        conn.executemany("UPDATE imports SET resolved_path = ? WHERE rowid = ?", imp_updates)

    # 2. Build symbol definition lookup for call resolution
    sym_rows = conn.execute(
        "SELECT name, rel_path, is_exported FROM symbols WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    defs_by_name: dict[str, list[str]] = {}
    exported_defs_by_name: dict[str, list[str]] = {}
    for sr in sym_rows:
        defs_by_name.setdefault(sr["name"], []).append(sr["rel_path"])
        if sr["is_exported"]:
            exported_defs_by_name.setdefault(sr["name"], []).append(sr["rel_path"])

    call_rows = conn.execute(
        "SELECT rowid, caller_path, callee_name FROM calls WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    call_updates: list[tuple[str, int]] = []
    call_edges: dict[str, set[str]] = {f: set() for f in known_files}

    for cr in call_rows:
        cpath = cr["caller_path"]
        cname = cr["callee_name"]
        resolved_target = ""
        # Priority 1: explicitly imported name
        if cname in imported_map.get(cpath, {}):
            resolved_target = imported_map[cpath][cname]
        else:
            # Priority 2: defined in a module imported by caller_path
            candidate_files = exported_defs_by_name.get(cname, [])
            imported_candidates = [cf for cf in candidate_files if cf in file_imports.get(cpath, set())]
            if len(imported_candidates) == 1:
                resolved_target = imported_candidates[0]
            elif cpath in defs_by_name.get(cname, []):
                resolved_target = cpath
            elif len(candidate_files) == 1:
                resolved_target = candidate_files[0]

        call_updates.append((resolved_target, int(cr["rowid"])))
        if resolved_target and resolved_target != cpath and resolved_target in known_files:
            call_edges.setdefault(cpath, set()).add(resolved_target)

    if call_updates:
        conn.executemany("UPDATE calls SET resolved_path = ? WHERE rowid = ?", call_updates)

    # 3. Combine directed dependency graph (A -> B means A depends on B)
    nodes = sorted(known_files)
    out_neighbors: dict[str, set[str]] = {n: set() for n in nodes}
    in_neighbors: dict[str, set[str]] = {n: set() for n in nodes}
    edge_count = 0
    for src in nodes:
        targets = (file_imports.get(src, set()) | call_edges.get(src, set())) - {src}
        out_neighbors[src] = targets
        edge_count += len(targets)
        for tgt in targets:
            if tgt in in_neighbors:
                in_neighbors[tgt].add(src)

    # 4. Compute PageRank (damping = 0.85, 25 iterations)
    n_count = len(nodes)
    damping = 0.85
    ranks: dict[str, float] = {n: 1.0 / n_count for n in nodes}
    base_rank = (1.0 - damping) / n_count
    for _ in range(25):
        dangling_sum = sum(ranks[n] for n in nodes if not out_neighbors[n])
        dangling_share = damping * dangling_sum / n_count
        next_ranks: dict[str, float] = {}
        for n in nodes:
            inbound = sum(ranks[src] / len(out_neighbors[src]) for src in in_neighbors[n])
            next_ranks[n] = base_rank + dangling_share + damping * inbound
        ranks = next_ranks

    conn.executemany(
        "UPDATE files SET pagerank = ?, fan_in = ?, fan_out = ? WHERE workspace_id = ? AND rel_path = ?",
        [
            (
                round(ranks[n], 6),
                len(in_neighbors[n]),
                len(out_neighbors[n]),
                workspace_id,
                n,
            )
            for n in nodes
        ],
    )
    return edge_count


def index_workspace(
    root_path: str | Path,
    *,
    force: bool = False,
    max_files: int = DEFAULT_MAX_FILES,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Incrementally scan and index *root_path*, returning summary statistics."""
    t0 = time.perf_counter()
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason}
    root = Path(root_path).expanduser().resolve()

    ws_id = workspace_id_for(root)
    discovered = _iter_workspace_source_files(root, max_files=max_files)

    with open_locus_db(db_path=db_path) as conn:
        existing_rows = conn.execute(
            "SELECT rel_path, mtime, size_bytes, sha256 FROM files WHERE workspace_id = ?",
            (ws_id,),
        ).fetchall()
        existing_by_path = {
            r["rel_path"]: (float(r["mtime"]), int(r["size_bytes"]), r["sha256"]) for r in existing_rows
        }

        discovered_paths = {rel for _, rel, _ in discovered}
        removed_paths = set(existing_by_path.keys()) - discovered_paths
        for dead in removed_paths:
            conn.execute("DELETE FROM files WHERE workspace_id = ? AND rel_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM symbols WHERE workspace_id = ? AND rel_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM imports WHERE workspace_id = ? AND src_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM calls WHERE workspace_id = ? AND caller_path = ?", (ws_id, dead))

        updated_files = 0
        for full_path, rel_posix, st in discovered:
            prev = existing_by_path.get(rel_posix)
            if not force and prev is not None and abs(prev[0] - st.st_mtime) < 1e-4 and prev[1] == st.st_size:
                continue
            try:
                raw_bytes = full_path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw_bytes[:4096]:
                continue
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if not force and prev is not None and prev[2] == digest:
                conn.execute(
                    "UPDATE files SET mtime = ?, size_bytes = ? WHERE workspace_id = ? AND rel_path = ?",
                    (st.st_mtime, st.st_size, ws_id, rel_posix),
                )
                continue

            source = raw_bytes.decode("utf-8", errors="replace")
            parsed = parse_source_file(rel_posix, source)
            if parsed is None:
                continue
            _store_parsed_file(conn, ws_id, parsed, st.st_mtime, st.st_size, digest)
            updated_files += 1

        need_relink = force or updated_files > 0 or bool(removed_paths) or not existing_by_path
        if need_relink:
            edge_count = _relink_and_rank(conn, ws_id)
            reconcile_drift_alerts(conn, ws_id)
        else:
            ws_row = conn.execute(
                "SELECT edge_count FROM workspaces WHERE workspace_id = ?", (ws_id,)
            ).fetchone()
            edge_count = int(ws_row["edge_count"]) if ws_row else 0

        sym_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM symbols WHERE workspace_id = ?", (ws_id,)
            ).fetchone()["c"]
        )
        file_count = len(discovered_paths)
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        conn.execute(
            """
            INSERT INTO workspaces (
                workspace_id, root_path, last_indexed_at, file_count, symbol_count, edge_count, scan_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                root_path = excluded.root_path,
                last_indexed_at = excluded.last_indexed_at,
                file_count = excluded.file_count,
                symbol_count = excluded.symbol_count,
                edge_count = excluded.edge_count,
                scan_ms = excluded.scan_ms
            """,
            (ws_id, str(root), time.time(), file_count, sym_count, edge_count, elapsed_ms),
        )

        if updated_files > 0 or removed_paths:
            conn.execute(
                """
                INSERT INTO activity_log (workspace_id, created_at, event_type, target_path, summary, details_json)
                VALUES (?, ?, 'scan', '', ?, ?)
                """,
                (
                    ws_id,
                    time.time(),
                    f"Indexed {file_count} files ({updated_files} updated, {sym_count} symbols) in {elapsed_ms}ms",
                    json.dumps({"updated": updated_files, "removed": len(removed_paths), "scan_ms": elapsed_ms}),
                ),
            )
            _prune_activity_log(conn, ws_id)

    return {
        "ok": True,
        "workspace_id": ws_id,
        "root_path": str(root),
        "file_count": file_count,
        "symbol_count": sym_count,
        "edge_count": edge_count,
        "updated_files": updated_files,
        "removed_files": len(removed_paths),
        "scan_ms": elapsed_ms,
    }


def reindex_edited_file(
    root_path: str | Path,
    edited_file: str | Path,
    *,
    tool_name: str = "edit",
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Re-parse one edited file, compute signature drift against callers, and refresh graph."""
    t0 = time.perf_counter()
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "skipped": True, "reason": "unsafe_workspace", "error": reason}
    root = Path(root_path).expanduser().resolve()
    ws_id = workspace_id_for(root)

    raw_target = Path(edited_file)
    if not raw_target.is_absolute():
        raw_target = root / raw_target
    if raw_target.is_symlink():
        return {"ok": False, "skipped": True, "reason": "symlink"}
    target = raw_target.resolve()

    try:
        rel_posix = target.relative_to(root).as_posix()
    except ValueError:
        return {"ok": False, "skipped": True, "reason": "outside_workspace"}

    if detect_language(rel_posix) is None:
        return {"ok": True, "skipped": True, "reason": "unsupported_extension"}

    with open_locus_db(db_path=db_path) as conn:
        # Ensure workspace baseline exists first
        ws_exists = conn.execute(
            "SELECT 1 FROM workspaces WHERE workspace_id = ?", (ws_id,)
        ).fetchone()

    if not ws_exists:
        index_workspace(root, db_path=db_path)
        return {"ok": True, "rel_path": rel_posix, "drift_alerts": [], "initial_scan": True}

    with open_locus_db(db_path=db_path) as conn:
        old_rows = conn.execute(
            """
            SELECT symbol_id, name, qualified_name, kind, rel_path, line_start, line_end,
                   signature, min_args, max_args, is_exported, docstring
            FROM symbols
            WHERE workspace_id = ? AND rel_path = ?
            """,
            (ws_id, rel_posix),
        ).fetchall()
        old_symbols = {
            r["qualified_name"]: ParsedSymbol(
                symbol_id=r["symbol_id"],
                name=r["name"],
                qualified_name=r["qualified_name"],
                kind=r["kind"],
                rel_path=r["rel_path"],
                line_start=int(r["line_start"]),
                line_end=int(r["line_end"]),
                signature=r["signature"],
                min_args=int(r["min_args"]),
                max_args=int(r["max_args"]),
                is_exported=bool(r["is_exported"]),
                docstring=r["docstring"],
            )
            for r in old_rows
        }

        if not target.exists():
            new_symbols: dict[str, ParsedSymbol] = {}
            detect_and_record_drift(
                conn, ws_id, rel_posix, old_symbols, new_symbols
            )
            conn.execute("DELETE FROM files WHERE workspace_id = ? AND rel_path = ?", (ws_id, rel_posix))
            conn.execute("DELETE FROM symbols WHERE workspace_id = ? AND rel_path = ?", (ws_id, rel_posix))
            conn.execute("DELETE FROM imports WHERE workspace_id = ? AND src_path = ?", (ws_id, rel_posix))
            conn.execute("DELETE FROM calls WHERE workspace_id = ? AND caller_path = ?", (ws_id, rel_posix))
        else:
            try:
                st = target.stat()
            except OSError:
                return {"ok": True, "skipped": True, "reason": "stat_failed"}
            if st.st_size > DEFAULT_MAX_FILE_BYTES:
                return {"ok": True, "skipped": True, "reason": "size_out_of_bounds"}
            try:
                raw_bytes = target.read_bytes()
            except OSError:
                return {"ok": True, "skipped": True, "reason": "read_failed"}
            if b"\x00" in raw_bytes[:4096]:
                return {"ok": True, "skipped": True, "reason": "binary_file"}
            digest = hashlib.sha256(raw_bytes).hexdigest()
            parsed = parse_source_file(rel_posix, raw_bytes.decode("utf-8", errors="replace"))
            if parsed is None:
                return {"ok": True, "skipped": True}
            new_symbols = {s.qualified_name: s for s in parsed.symbols}
            detect_and_record_drift(conn, ws_id, rel_posix, old_symbols, new_symbols)
            _store_parsed_file(conn, ws_id, parsed, st.st_mtime, st.st_size, digest)

        edge_count = _relink_and_rank(conn, ws_id)
        alerts = [
            alert for alert in reconcile_drift_alerts(conn, ws_id)
            if alert.edited_path == rel_posix
        ]
        file_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM files WHERE workspace_id = ?", (ws_id,)).fetchone()["c"]
        )
        sym_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM symbols WHERE workspace_id = ?", (ws_id,)).fetchone()["c"]
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        conn.execute(
            """
            UPDATE workspaces
            SET last_indexed_at = ?, file_count = ?, symbol_count = ?, edge_count = ?
            WHERE workspace_id = ?
            """,
            (time.time(), file_count, sym_count, edge_count, ws_id),
        )

        summary = f"{tool_name} updated {rel_posix}"
        if alerts:
            summary += f" ({len(alerts)} signature drift alert(s))"
        conn.execute(
            """
            INSERT INTO activity_log (workspace_id, created_at, event_type, target_path, summary, details_json)
            VALUES (?, ?, 'file_edit', ?, ?, ?)
            """,
            (
                ws_id,
                time.time(),
                rel_posix,
                summary,
                json.dumps(
                    {
                        "tool": tool_name,
                        "elapsed_ms": elapsed_ms,
                        "drift_count": len(alerts),
                    }
                ),
            ),
        )
        _prune_activity_log(conn, ws_id)

    return {
        "ok": True,
        "rel_path": rel_posix,
        "elapsed_ms": elapsed_ms,
        "drift_alerts": [a.to_dict() for a in alerts],
    }
