"""Incremental workspace scanner, cross-file import/call resolver, and PageRank updater."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import posixpath
import sqlite3
import stat
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
_PYTHON_PARSER_REVISION = 6

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


def _path_exclusion_reason(
    rel_posix: str, patterns: list[str], *, is_dir: bool = False,
    parents_checked: bool = False,
) -> str:
    """Apply the same lightweight path filters to scans and edit notifications."""
    parts = rel_posix.split("/")
    # A scan already prunes excluded parents before descending. Individual
    # edit notifications have no such guarantee and must inspect every part.
    start = len(parts) - 1 if parents_checked else 0
    for i in range(start, len(parts)):
        part = parts[i]
        if part.startswith("."):
            return "hidden_path"
        if (is_dir or i < len(parts) - 1) and part in SKIP_DIR_NAMES:
            return "excluded_directory"
        prefix = rel_posix if i == len(parts) - 1 else "/".join(parts[: i + 1])
        if _matches_gitignore(prefix, part, patterns):
            return "gitignored"
    return ""


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
            d_rel = f"{rel_dir}/{d}".lstrip("/")
            if _path_exclusion_reason(d_rel, gi_patterns, is_dir=True, parents_checked=True):
                continue
            d_full = cur_dir / d
            if d_full.is_symlink():
                continue
            kept_dirs.append(d)
        dirnames[:] = kept_dirs

        for fname in sorted(filenames):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in LANGUAGE_BY_EXT:
                continue
            f_full = cur_dir / fname
            if f_full.is_symlink():
                continue
            rel_posix = f"{rel_dir}/{fname}".lstrip("/")
            if _path_exclusion_reason(rel_posix, gi_patterns, parents_checked=True):
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


def _join_python_spec(module: str, name: str) -> str:
    return module + ("" if module.endswith(".") else ".") + name


class _PythonModuleIndex:
    """Map runtime module paths once, retaining ambiguity instead of guessing.

    Root, src/ and lib/ layouts are recognized. A duplicate module across source
    roots is unresolved; Python stubs never override an existing .py runtime file.
    Relative imports stay inside their inferred source root.
    """

    def __init__(self, paths: set[str]) -> None:
        self.paths = paths
        self.modules: dict[str, set[str]] = {}
        self.by_stem: dict[str, set[str]] = {}
        for path in sorted(paths):
            if not path.endswith((".py", ".pyi")):
                continue
            stem = path.rsplit(".", 1)[0]
            if path.endswith(".pyi") and stem + ".py" in paths:
                continue
            if stem.endswith("/__init__"):
                stem = stem[:-9]
            self.by_stem.setdefault(stem, set()).add(path)
            roots = [stem]
            if stem.startswith(("src/", "lib/")):
                roots.append(stem.split("/", 1)[1])
            for name in roots:
                self.modules.setdefault(name.replace("/", "."), set()).add(path)

    def resolve(self, src: str, spec: str) -> str:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            directory = posixpath.dirname(src).split("/")
            if directory == [""]:
                return ""
            floor = 1 if directory[0] in {"src", "lib"} else 0
            up = level - 1
            if len(directory) - up <= floor:
                return ""
            base = directory[:len(directory) - up]
            tail = spec[level:]
            if tail:
                base.extend(tail.split("."))
            matches = self.by_stem.get("/".join(base), set())
        else:
            matches = self.modules.get(spec, set())
        return next(iter(matches)) if len(matches) == 1 else ""

    def _module_from(self, src: str, module: str, name: str, metadata: dict,
                     seen: frozenset = frozenset()) -> str:
        key = (src, module, name)
        if key in seen or len(seen) >= 8:
            return ""
        seen = seen | {key}
        package = self.resolve(src, module)
        info = metadata.get(package, {})
        if info.get("dynamic"):
            return ""
        kind = info.get("bindings", {}).get(name)
        binding = info.get("imports", {}).get(name, {})
        if kind == "module":
            imported = binding.get("module", "")
            return self.resolve(package, imported if binding.get("name") else imported.split(".")[0])
        if kind == "from":
            if self.resolve(package, binding["module"]) == package and binding["name"] == name:
                return self.resolve(package, _join_python_spec(binding["module"], name))
            return self._module_from(package, binding["module"], binding["name"], metadata, seen)
        if kind is not None:
            return ""
        return self.resolve(src, _join_python_spec(module, name))

    def _export(self, path: str, name: str, metadata: dict,
                seen: frozenset = frozenset()) -> tuple[str, str, str]:
        """Follow static re-exports to ``(path, name, kind)``; an empty path is unknown."""
        key = (path, name)
        if not path or key in seen or len(seen) >= 8:
            return "", name, ""
        info = metadata.get(path, {})
        if info.get("dynamic"):
            return "", name, ""
        kind = info.get("bindings", {}).get(name)
        if kind == "from":
            binding = info.get("imports", {}).get(name, {})
            target = self.resolve(path, binding.get("module", ""))
            return self._export(target, binding.get("name", name), metadata, seen | {key})
        if kind is None:
            if "__getattr__" in info.get("bindings", {}):
                return "", name, ""
            # Keep a missing name linked to its module so deletion alerts survive.
            return path, name, "missing"
        # Assignments and decorated/conditional definitions are unknown.
        return (path, name, kind) if kind in ("function", "class") else ("", name, "")

    def _class_member(self, module_path: str, class_name: str, member: str,
                      metadata: dict, instance: bool = False) -> tuple[str, str, str]:
        """Resolve ``Class.member()``, or ``obj.member()`` on an instance built from ``Class(...)``.

        Through the class, only static and class methods have a receiver-free
        contract. On a directly constructed instance the exact type is known, so a
        plain method binds too, unless the instance could shadow or intercept it.
        """
        path, canonical, kind = self._export(module_path, class_name, metadata)
        classes = metadata.get(path, {}).get("classes", {}) if kind == "class" else {}
        accepted = {"staticmethod", "classmethod", "function"} if instance else {"staticmethod", "classmethod"}
        shadowed: set[str] = set()
        owner = canonical
        for _ in range(8):
            info = classes.get(owner)
            if not info or (instance and not info.get("instance_safe")):
                break
            shadowed.update(info.get("instance_attributes", ()))
            if instance and member in shadowed:
                break
            member_kind = info["members"].get(member)
            if member_kind in accepted:
                return path, member, f"{owner}.{member}"
            if member_kind is not None:
                break
            if info["closed"]:
                # Nothing left to inherit from: keep the absent name linked, like
                # a removed function, so its deletion alert survives.
                return path, member, f"{canonical}.{member}"
            # Single inheritance within one module has an unambiguous lookup order.
            owner = info.get("base")
        return "", member, ""

    def call_binding(self, src: str, shape: dict, metadata: dict[str, dict]) -> tuple[str, str, str]:
        """Return ``(path, name, qualified name)`` of a statically proven callee.

        A call to a class resolves to the class symbol, whose contract is its
        constructor. Instance-method calls need the receiver's type and stay unresolved.
        """
        attrs = shape.get("attribute_path", [])
        name = shape.get("import_name", "")
        symbol = attrs[-1] if attrs else name
        if not shape.get("direct_import") or len(attrs) > 2:
            return "", symbol, ""
        module = shape.get("import_module", "")
        instance = shape.get("receiver") == "instance"
        if shape.get("import_kind") == "local":
            # The calling module's own definition; the same export rules decide provability.
            if len(attrs) > 1:
                return "", symbol, ""
            if attrs:
                return self._class_member(src, name, attrs[0], metadata, instance)
            path, canonical, _ = self._export(src, name, metadata)
            return path, canonical, canonical if path else ""
        if shape.get("import_kind") == "from" and attrs:
            target = self._module_from(src, module, name, metadata)
            if not target:
                # Not a module: the imported name may be a class whose member is called.
                if len(attrs) != 1:
                    return "", symbol, ""
                return self._class_member(self.resolve(src, module), name, attrs[0], metadata, instance)
        else:
            target = self.resolve(src, module)
        if len(attrs) == 2:
            return self._class_member(target, attrs[0], attrs[1], metadata, instance)
        if instance:
            return "", symbol, ""  # a receiver that is not a class instance
        path, canonical, _ = self._export(target, symbol, metadata)
        return path, canonical, canonical if path else ""

    def call_target(self, src: str, shape: dict, metadata: dict[str, dict]) -> tuple[str, str]:
        return self.call_binding(src, shape, metadata)[:2]


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
        modules = _PythonModuleIndex(known_files)
        direct = modules.resolve(src_path, spec)
        if direct:
            return direct
        targets = {modules.resolve(src_path, _join_python_spec(spec, name)) for name in imported_names}
        targets.discard("")
        return next(iter(targets)) if len(targets) == 1 else ""

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
            line_count, is_test, parser_revision, python_module_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
            lang = excluded.lang,
            mtime = excluded.mtime,
            size_bytes = excluded.size_bytes,
            sha256 = excluded.sha256,
            line_count = excluded.line_count,
            is_test = excluded.is_test,
            parser_revision = excluded.parser_revision,
            python_module_json = excluded.python_module_json
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
            _PYTHON_PARSER_REVISION if parsed.lang == "python" else 0,
            json.dumps(parsed.python_module) if parsed.python_module is not None else None,
        ),
    )

    if parsed.symbols:
        conn.executemany(
            """
            INSERT OR REPLACE INTO symbols (
                workspace_id, symbol_id, name, qualified_name, kind, rel_path,
                line_start, line_end, signature, min_args, max_args, is_exported, docstring,
                python_signature_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(s.python_signature) if s.python_signature is not None else None,
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
            INSERT INTO calls (
                workspace_id, caller_path, caller_symbol, callee_name, resolved_path,
                line_no, arg_count, python_call_json
            ) VALUES (?, ?, ?, ?, '', ?, ?, ?)
            """,
            [
                (
                    workspace_id,
                    c.caller_path,
                    c.caller_symbol,
                    c.callee_name,
                    c.line_no,
                    c.arg_count,
                    json.dumps(c.python_call) if c.python_call is not None else None,
                )
                for c in parsed.calls
            ],
        )


def _relink_and_rank(conn: sqlite3.Connection, workspace_id: str) -> int:
    """Resolve cross-file imports & calls and compute PageRank + fan-in/fan-out."""
    file_rows = conn.execute(
        "SELECT rel_path, lang, pagerank, fan_in, fan_out, python_module_json FROM files WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    known_files = {r["rel_path"] for r in file_rows}
    old_file_stats = {r["rel_path"]: (r["pagerank"], r["fan_in"], r["fan_out"]) for r in file_rows}
    lang_by_file = {r["rel_path"]: r["lang"] for r in file_rows}
    if not known_files:
        return 0

    # Deleted targets retained by open alerts keep enough identity to reconcile
    # existing calls, without being reintroduced as graph nodes.
    tombstones = {r[0] for r in conn.execute(
        "SELECT DISTINCT edited_path FROM drift_alerts WHERE workspace_id = ? AND resolved = 0", (workspace_id,)
    )}
    python_modules = _PythonModuleIndex(known_files | tombstones)
    python_metadata = {r["rel_path"]: json.loads(r["python_module_json"])
                       for r in file_rows if r["python_module_json"]}

    # 1. Resolve imports
    imp_rows = conn.execute(
        "SELECT rowid, src_path, target_spec, imported_names, resolved_path FROM imports WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()

    imp_updates: list[tuple[str, int]] = []
    imported_map: dict[str, dict[str, str]] = {f: {} for f in known_files}
    file_imports: dict[str, set[str]] = {f: set() for f in known_files}

    for r in imp_rows:
        src = r["src_path"]
        lang = lang_by_file.get(src, "python")
        names = [n for n in (r["imported_names"] or "").split(",") if n]
        if lang == "python":
            resolved = python_modules.resolve(src, r["target_spec"])
            if len(names) == 1:
                resolved = python_modules._module_from(src, r["target_spec"], names[0], python_metadata) or resolved
        else:
            resolved = _resolve_import_spec(src, lang, r["target_spec"], known_files, names)
        if resolved != r["resolved_path"]:
            imp_updates.append((resolved, int(r["rowid"])))
        if resolved and resolved in known_files:
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
        "SELECT rowid, caller_path, callee_name, callee_qualname, resolved_path, python_call_json "
        "FROM calls WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchall()
    call_updates: list[tuple[str, str, str, int]] = []
    call_edges: dict[str, set[str]] = {f: set() for f in known_files}

    for cr in call_rows:
        cpath = cr["caller_path"]
        cname = cr["callee_name"]
        resolved_target = ""
        # Exact symbol identity of a proven Python callee ("f", "Class" for a
        # constructor call, "Class.method"); empty for approximate resolution.
        qualname = ""
        shape = json.loads(cr["python_call_json"]) if cr["python_call_json"] else None
        if lang_by_file.get(cpath) == "python" and shape and shape.get("binding_version") == 2:
            resolved_target, canonical_name, qualname = python_modules.call_binding(cpath, shape, python_metadata)
            if canonical_name:
                cname = canonical_name
        # Legacy/non-Python structural calls retain their approximate graph resolution.
        elif cname in imported_map.get(cpath, {}):
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

        if (resolved_target != cr["resolved_path"] or cname != cr["callee_name"]
                or qualname != cr["callee_qualname"]):
            call_updates.append((resolved_target, cname, qualname, int(cr["rowid"])))
        if resolved_target and resolved_target != cpath and resolved_target in known_files:
            call_edges.setdefault(cpath, set()).add(resolved_target)

    if call_updates:
        conn.executemany(
            "UPDATE calls SET resolved_path = ?, callee_name = ?, callee_qualname = ? WHERE rowid = ?", call_updates)

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

    rank_updates = []
    for n in nodes:
        stats = (round(ranks[n], 6), len(in_neighbors[n]), len(out_neighbors[n]))
        if stats != old_file_stats[n]:
            rank_updates.append((*stats, workspace_id, n))
    if rank_updates:
        conn.executemany(
            "UPDATE files SET pagerank = ?, fan_in = ?, fan_out = ? WHERE workspace_id = ? AND rel_path = ?",
            rank_updates,
        )
    return edge_count


def _scan_status(conn: sqlite3.Connection, workspace_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT scan_status_json FROM workspaces WHERE workspace_id = ?", (workspace_id,)).fetchone()
    if row is None:
        return {}
    try:
        result = json.loads(row[0])
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def _retained_scan_path(root: Path, rel_path: str, patterns: list[str], truncated: bool) -> tuple[bool, str]:
    """Inspect metadata only: a skipped existing path is not a removed caller."""
    target = root / rel_path
    probe = target
    while probe != root and root in probe.parents:
        if probe.is_symlink():
            return True, "Symbolic link excluded; last-valid indexed source retained."
        probe = probe.parent
    try:
        metadata = target.stat()
    except FileNotFoundError:
        return False, ""
    except OSError:
        return True, "Source could not be inspected; last-valid indexed source retained."
    if not stat.S_ISREG(metadata.st_mode):
        return True, "Path is no longer a regular source file; last-valid indexed source retained."
    if metadata.st_size == 0:
        # An observed empty file proves that its previous calls were removed.
        return False, ""
    exclusion = _path_exclusion_reason(rel_path, patterns)
    if exclusion:
        return True, f"Source excluded by scan rules ({exclusion}); last-valid indexed source retained."
    if metadata.st_size > DEFAULT_MAX_FILE_BYTES:
        return True, "Source exceeds the 512 KiB indexing limit; last-valid indexed source retained."
    if truncated:
        return True, "Outside the current file-limited scan; last-valid indexed source retained."
    return True, "Source was not reached by the scan; last-valid indexed source retained."


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
    max_files = max(1, int(max_files))
    discovered = _iter_workspace_source_files(root, max_files=max_files + 1)
    truncated = len(discovered) > max_files
    discovered = discovered[:max_files]

    with open_locus_db(db_path=db_path) as conn:
        existing_rows = conn.execute(
            """
            SELECT rel_path, mtime, size_bytes, sha256, lang, parser_revision
            FROM files WHERE workspace_id = ?
            """,
            (ws_id,),
        ).fetchall()
        existing_by_path = {r["rel_path"]: r for r in existing_rows}
        previous_status = _scan_status(conn, ws_id)
        previous_stale = {entry["path"]: entry for entry in previous_status.get("stale_files", [])}
        stale_map: dict[str, dict[str, str]] = {}

        discovered_paths = {rel for _, rel, _ in discovered}
        removed_paths = set()
        retained_paths = set()
        patterns = _load_gitignore_patterns(root)
        for path in set(existing_by_path) - discovered_paths:
            retain, reason = _retained_scan_path(root, path, patterns, truncated)
            if retain:
                retained_paths.add(path)
                if reason:
                    stale_map[path] = {"path": path, "reason": reason}
                elif path in previous_stale:
                    stale_map[path] = previous_stale[path]
            else:
                removed_paths.add(path)
        for dead in removed_paths:
            conn.execute("DELETE FROM files WHERE workspace_id = ? AND rel_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM symbols WHERE workspace_id = ? AND rel_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM imports WHERE workspace_id = ? AND src_path = ?", (ws_id, dead))
            conn.execute("DELETE FROM calls WHERE workspace_id = ? AND caller_path = ?", (ws_id, dead))

        updated_files = 0
        retained_error_paths = {
            entry["path"] for entry in previous_status.get("parse_errors", [])
            if entry["path"] not in discovered_paths
            and _retained_scan_path(root, entry["path"], patterns, truncated)[0]
        }
        error_map = {entry["path"]: entry for entry in previous_status.get("parse_errors", [])
                     if entry["path"] in discovered_paths or entry["path"] in retained_error_paths}
        for full_path, rel_posix, st in discovered:
            prev = existing_by_path.get(rel_posix)
            if prev is not None and prev["lang"] == "python" and prev["parser_revision"] < _PYTHON_PARSER_REVISION:
                # Refresh legacy Python metadata even when source contents did not change.
                # Leave the previous rows intact until parsing succeeds.
                prev = None
            if (
                not force and rel_posix not in previous_stale and prev is not None
                and abs(prev["mtime"] - st.st_mtime) < 1e-4 and prev["size_bytes"] == st.st_size
            ):
                continue
            try:
                raw_bytes = full_path.read_bytes()
            except OSError:
                if rel_posix in existing_by_path:
                    stale_map[rel_posix] = {"path": rel_posix, "reason": "Source could not be read; last-valid indexed source retained."}
                continue
            if b"\x00" in raw_bytes[:4096]:
                if rel_posix in existing_by_path:
                    stale_map[rel_posix] = {"path": rel_posix, "reason": "Binary content excluded; last-valid indexed source retained."}
                continue
            digest = hashlib.sha256(raw_bytes).hexdigest()
            if not force and prev is not None and prev["sha256"] == digest:
                error_map.pop(rel_posix, None)
                conn.execute(
                    "UPDATE files SET mtime = ?, size_bytes = ? WHERE workspace_id = ? AND rel_path = ?",
                    (st.st_mtime, st.st_size, ws_id, rel_posix),
                )
                continue

            source = raw_bytes.decode("utf-8", errors="replace")
            parsed = parse_source_file(rel_posix, source)
            if parsed is None:
                continue
            if parsed.parse_error:
                error_map[rel_posix] = {"path": rel_posix, "error": parsed.parse_error}
                continue
            error_map.pop(rel_posix, None)
            _store_parsed_file(conn, ws_id, parsed, st.st_mtime, st.st_size, digest)
            updated_files += 1

        parse_errors = sorted(error_map.values(), key=lambda item: item["path"])
        # Reconciliation must see the coverage from this transaction, not the
        # prior scan, before it decides whether an established risk was repaired.
        conn.execute("UPDATE workspaces SET scan_status_json = ? WHERE workspace_id = ?", (
            json.dumps({**previous_status, "parse_errors": parse_errors,
                        "stale_files": sorted(stale_map.values(), key=lambda entry: entry["path"])}), ws_id,
        ))
        need_relink = force or updated_files > 0 or bool(removed_paths) or not existing_by_path
        if need_relink:
            edge_count = _relink_and_rank(conn, ws_id)
        else:
            ws_row = conn.execute(
                "SELECT edge_count FROM workspaces WHERE workspace_id = ?", (ws_id,)
            ).fetchone()
            edge_count = int(ws_row["edge_count"]) if ws_row else 0
        if need_relink or stale_map != previous_stale or parse_errors != previous_status.get("parse_errors", []):
            reconcile_drift_alerts(conn, ws_id)

        sym_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM symbols WHERE workspace_id = ?", (ws_id,)
            ).fetchone()["c"]
        )
        file_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM files WHERE workspace_id = ?", (ws_id,)).fetchone()["c"]
        )
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        conn.execute(
            """
            INSERT INTO workspaces (
                workspace_id, root_path, last_indexed_at, file_count, symbol_count, edge_count, scan_ms, scan_status_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace_id) DO UPDATE SET
                root_path = excluded.root_path,
                last_indexed_at = excluded.last_indexed_at,
                file_count = excluded.file_count,
                symbol_count = excluded.symbol_count,
                edge_count = excluded.edge_count,
                scan_ms = excluded.scan_ms,
                scan_status_json = excluded.scan_status_json
            """,
            (ws_id, str(root), time.time(), file_count, sym_count, edge_count, elapsed_ms,
             json.dumps({"truncated": truncated, "max_files": max_files, "parse_errors": parse_errors,
                         "stale_files": sorted(stale_map.values(), key=lambda entry: entry["path"]),
                         "last_indexed_at": time.time(), "indexed_files": file_count})),
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
        "parse_errors": parse_errors,
        "stale_files": sorted(stale_map.values(), key=lambda entry: entry["path"]),
        "truncated": truncated,
        "max_files": max_files,
        "scan_ms": elapsed_ms,
    }


def _prepare_edited_file(root: Path, edited_file: str | Path, patterns: list[str]) -> dict[str, Any]:
    """Read and parse one changed path without mutating the index."""
    if not isinstance(edited_file, (str, Path)):
        return {"ok": False, "skipped": True, "reason": "invalid_path"}
    raw_target = Path(edited_file)
    if not raw_target.is_absolute():
        raw_target = root / raw_target
    raw_target = Path(os.path.abspath(raw_target))
    try:
        rel = raw_target.relative_to(root).as_posix()
    except ValueError:
        # macOS exposes the same temporary directory via /var and /private/var.
        # Normalize an equivalent workspace prefix, without following symlinks
        # inside the workspace before they can be rejected below.
        prefix = None
        for parent in raw_target.parents:
            try:
                if parent.resolve() == root:
                    prefix = parent
                    break
            except OSError:
                continue
        if prefix is None:
            return {"ok": False, "skipped": True, "reason": "outside_workspace"}
        rel = raw_target.relative_to(prefix).as_posix()
        raw_target = root / rel
    outcome: dict[str, Any] = {"ok": True, "rel_path": rel}
    probe = raw_target
    while probe != root and root in probe.parents:
        if probe.is_symlink():
            return {**outcome, "skipped": True, "reason": "symlink"}
        probe = probe.parent
    target = raw_target.resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError:
        return {"ok": False, "skipped": True, "reason": "outside_workspace"}
    if detect_language(rel) is None:
        return {**outcome, "skipped": True, "reason": "unsupported_extension"}
    if not target.exists():
        return {**outcome, "deleted": True}
    exclusion = _path_exclusion_reason(rel, patterns)
    if exclusion:
        return {**outcome, "skipped": True, "reason": exclusion}
    try:
        st = target.stat()
    except OSError:
        return {**outcome, "skipped": True, "reason": "stat_failed"}
    if st.st_size > DEFAULT_MAX_FILE_BYTES:
        return {**outcome, "skipped": True, "reason": "size_out_of_bounds"}
    try:
        raw = target.read_bytes()
    except OSError:
        return {**outcome, "skipped": True, "reason": "read_failed"}
    if b"\x00" in raw[:4096]:
        return {**outcome, "skipped": True, "reason": "binary_file"}
    parsed = parse_source_file(rel, raw.decode("utf-8", errors="replace"))
    if parsed is None:
        return {**outcome, "skipped": True, "reason": "unsupported_extension"}
    if parsed.parse_error:
        return {**outcome, "skipped": True, "reason": "parse_error", "error": parsed.parse_error}
    return {**outcome, "_parsed": parsed, "_stat": st, "_sha": hashlib.sha256(raw).hexdigest()}


def _old_symbols(conn: sqlite3.Connection, workspace_id: str, path: str) -> dict[str, ParsedSymbol]:
    rows = conn.execute(
        "SELECT * FROM symbols WHERE workspace_id = ? AND rel_path = ?", (workspace_id, path)
    ).fetchall()
    return {r["qualified_name"]: ParsedSymbol(
        symbol_id=r["symbol_id"], name=r["name"], qualified_name=r["qualified_name"], kind=r["kind"],
        rel_path=r["rel_path"], line_start=int(r["line_start"]), line_end=int(r["line_end"]),
        signature=r["signature"], min_args=int(r["min_args"]), max_args=int(r["max_args"]),
        is_exported=bool(r["is_exported"]), docstring=r["docstring"],
        python_signature=json.loads(r["python_signature_json"]) if r["python_signature_json"] else None,
    ) for r in rows}


def reindex_edited_files(
    root_path: str | Path,
    edited_files: list[str | Path],
    *,
    tool_name: str = "edit",
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Apply one tool's changed paths as a batch, relinking the graph exactly once.

    All drift baselines are collected from the previous index before any new file
    is stored. One final reconciliation prevents transient alerts when a single
    patch repairs both a definition and its callers. Invalid source preserves the
    last valid rows and contributes a separate per-file indexing warning.
    """
    started = time.perf_counter()
    safe, reason = validate_safe_workspace_root(root_path)
    if not safe:
        return {"ok": False, "skipped": True, "reason": "unsafe_workspace", "error": reason,
                "outcomes": [], "drift_alerts": [], "parse_errors": []}
    root = Path(root_path).expanduser().resolve()
    ws_id = workspace_id_for(root)
    patterns = _load_gitignore_patterns(root)
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in edited_files:
        item = _prepare_edited_file(root, path, patterns)
        key = item.get("rel_path", str(path))
        if key not in seen:
            seen.add(key)
            prepared.append(item)
    public = [{k: v for k, v in item.items() if not k.startswith("_")} for item in prepared]
    prepared_paths = {item["rel_path"] for item in prepared if item.get("rel_path")}
    parse_errors = [{"path": item["rel_path"], "error": item["error"]}
                    for item in prepared if item.get("reason") == "parse_error"]
    valid = [item for item in prepared if item.get("ok") and not item.get("skipped")]
    with open_locus_db(db_path=db_path) as conn:
        exists = conn.execute("SELECT 1 FROM workspaces WHERE workspace_id = ?", (ws_id,)).fetchone()
    if not exists:
        scan = index_workspace(root, db_path=db_path)
        for item in public:
            item["initial_scan"] = True
        return {"ok": scan.get("ok", False), "initial_scan": True, "outcomes": public,
                "edited_paths": [item["rel_path"] for item in valid], "drift_alerts": [],
                "parse_errors": scan.get("parse_errors", parse_errors),
                "stale_files": [entry for entry in scan.get("stale_files", []) if entry["path"] in prepared_paths],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)}

    with open_locus_db(db_path=db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        newest_alert = conn.execute("SELECT COALESCE(MAX(id), 0) FROM drift_alerts").fetchone()[0]
        previous_alerts = {
            (row["edited_path"], row["symbol_name"]): (
                row["severity"], row["new_signature"], row["reason"], json.loads(row["affected_callers_json"]),
            ) for row in conn.execute(
                "SELECT edited_path, symbol_name, severity, new_signature, reason, affected_callers_json "
                "FROM drift_alerts WHERE workspace_id = ? AND resolved = 0", (ws_id,),
            )
        }
        # Every comparison sees the same old index, irrespective of patch order.
        for item in valid:
            path = item["rel_path"]
            parsed = item.get("_parsed")
            new_symbols = {sym.qualified_name: sym for sym in parsed.symbols} if parsed else {}
            detect_and_record_drift(conn, ws_id, path, _old_symbols(conn, ws_id, path), new_symbols)
        for item in valid:
            path = item["rel_path"]
            if item.get("deleted"):
                for table, column in (("files", "rel_path"), ("symbols", "rel_path"),
                                      ("imports", "src_path"), ("calls", "caller_path")):
                    conn.execute(f"DELETE FROM {table} WHERE workspace_id = ? AND {column} = ?", (ws_id, path))
            else:
                st = item["_stat"]
                _store_parsed_file(conn, ws_id, item["_parsed"], st.st_mtime, st.st_size, item["_sha"])

        changed = {item["rel_path"] for item in valid}
        alerts = []
        edge_count = None
        file_count = conn.execute("SELECT COUNT(*) FROM files WHERE workspace_id = ?", (ws_id,)).fetchone()[0]
        symbol_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE workspace_id = ?", (ws_id,)).fetchone()[0]
        status = _scan_status(conn, ws_id)
        errors = {entry["path"]: entry for entry in status.get("parse_errors", [])}
        stale = {entry["path"]: entry for entry in status.get("stale_files", [])}
        for path in changed:
            errors.pop(path, None)
            stale.pop(path, None)
        errors.update({entry["path"]: entry for entry in parse_errors})
        for item in prepared:
            path = item.get("rel_path")
            if path and item.get("skipped") and item.get("reason") != "parse_error":
                if conn.execute("SELECT 1 FROM files WHERE workspace_id = ? AND rel_path = ?", (ws_id, path)).fetchone():
                    stale[path] = {"path": path, "reason": f"Source skipped ({item['reason']}); last-valid indexed source retained."}
            elif path and item.get("reason") == "parse_error":
                # The separate parse diagnostic now describes this retained row.
                stale.pop(path, None)
        now = time.time()
        status.update(parse_errors=sorted(errors.values(), key=lambda entry: entry["path"]),
                      stale_files=sorted(stale.values(), key=lambda entry: entry["path"]),
                      last_indexed_at=now, indexed_files=file_count)
        conn.execute("UPDATE workspaces SET scan_status_json = ? WHERE workspace_id = ?", (json.dumps(status), ws_id))
        if valid:
            edge_count = _relink_and_rank(conn, ws_id)
        # Coverage loss alone can downgrade an existing alert even when every
        # edited path was skipped and no graph rows changed.
        alerts = [alert for alert in reconcile_drift_alerts(conn, ws_id)
                  if alert.edited_path in changed
                  or any(caller["path"] in changed for caller in alert.affected_callers)
                  or previous_alerts.get((alert.edited_path, alert.symbol_name)) != (
                      alert.severity, alert.new_signature, alert.reason, alert.affected_callers,
                  )]
        # An edit that changes a definition and its own module's callers together was
        # never a break; do not leave a closed record for an alert nobody saw.
        for row in conn.execute(
            "SELECT id, edited_path, affected_callers_json FROM drift_alerts "
            "WHERE workspace_id = ? AND id > ? AND resolved = 1", (ws_id, newest_alert),
        ).fetchall():
            recorded = json.loads(row["affected_callers_json"])
            if recorded and all(caller.get("path") == row["edited_path"] for caller in recorded):
                conn.execute("DELETE FROM drift_alerts WHERE id = ?", (int(row["id"]),))
        conn.execute(
            "UPDATE workspaces SET last_indexed_at = ?, file_count = ?, symbol_count = ?, "
            "edge_count = COALESCE(?, edge_count), scan_status_json = ? WHERE workspace_id = ?",
            (now, file_count, symbol_count, edge_count, json.dumps(status), ws_id),
        )
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        if valid:
            summary = f"{tool_name} updated " + (next(iter(changed)) if len(changed) == 1 else f"{len(changed)} files")
            if alerts:
                summary += f" ({len(alerts)} signature drift alert(s))"
            conn.execute(
                "INSERT INTO activity_log (workspace_id, created_at, event_type, target_path, summary, details_json) "
                "VALUES (?, ?, 'file_edit', ?, ?, ?)",
                (ws_id, now, next(iter(changed)) if len(changed) == 1 else "", summary,
                 json.dumps({"tool": tool_name, "edited_paths": sorted(changed), "elapsed_ms": elapsed,
                             "drift_count": len(alerts), "parse_errors": parse_errors})),
            )
            _prune_activity_log(conn, ws_id)
    return {"ok": True, "outcomes": public, "edited_paths": sorted(changed),
            "drift_alerts": [alert.to_dict() for alert in alerts], "parse_errors": parse_errors,
            "stale_files": [stale[path] for path in sorted(prepared_paths) if path in stale],
            "elapsed_ms": elapsed}


def reindex_edited_file(
    root_path: str | Path,
    edited_file: str | Path,
    *,
    tool_name: str = "edit",
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Backward-compatible one-file wrapper around the atomic batch path."""
    batch = reindex_edited_files(root_path, [edited_file], tool_name=tool_name, db_path=db_path)
    outcomes = batch.get("outcomes", [])
    if not outcomes:
        return {k: v for k, v in batch.items() if k != "outcomes"}
    result = dict(outcomes[0])
    if batch.get("stale_files"):
        result["stale_files"] = batch["stale_files"]
    if not result.get("skipped"):
        result.update(drift_alerts=batch.get("drift_alerts", []), elapsed_ms=batch.get("elapsed_ms", 0))
        if batch.get("initial_scan"):
            result["parse_errors"] = batch.get("parse_errors", [])
    return result
