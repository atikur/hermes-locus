"""Graph intelligence queries: RepoMap, Blast-Radius BFS, Tarjan Cycles, Hotspots & Visual Graph."""

from __future__ import annotations

import json
import math
import posixpath
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any, Optional

from .drift import list_open_drift_alerts
from .indexer import index_workspace, validate_safe_workspace_root
from .storage import open_locus_db, workspace_id_for


def _ensure_indexed(root_path: str | Path, db_path: Optional[str | Path] = None) -> tuple[Path, str]:
    root = Path(root_path).expanduser().resolve()
    ws_id = workspace_id_for(root)
    needs_scan = False
    with open_locus_db(db_path=db_path) as conn:
        row = conn.execute(
            "SELECT file_count FROM workspaces WHERE workspace_id = ?", (ws_id,)
        ).fetchone()
        if row is None:
            needs_scan = True
    if needs_scan and root.is_dir():
        index_workspace(root, db_path=db_path)
    return root, ws_id


def _load_dependency_maps(
    conn: sqlite3.Connection, ws_id: str
) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, dict[str, Any]]]:
    """Return (out_deps, reverse_deps, file_meta).

    out_deps[A] contains B when A imports or calls B.
    reverse_deps[B] contains A when A depends on B (i.e. editing B impacts A!).
    """
    f_rows = conn.execute(
        """
        SELECT rel_path, lang, line_count, pagerank, fan_in, fan_out, is_test
        FROM files WHERE workspace_id = ?
        """,
        (ws_id,),
    ).fetchall()
    meta: dict[str, dict[str, Any]] = {
        r["rel_path"]: {
            "rel_path": r["rel_path"],
            "lang": r["lang"],
            "line_count": int(r["line_count"]),
            "pagerank": float(r["pagerank"]),
            "fan_in": int(r["fan_in"]),
            "fan_out": int(r["fan_out"]),
            "is_test": bool(r["is_test"]),
        }
        for r in f_rows
    }
    out_deps: dict[str, set[str]] = {p: set() for p in meta}
    rev_deps: dict[str, set[str]] = {p: set() for p in meta}

    for r in conn.execute(
        "SELECT DISTINCT src_path, resolved_path FROM imports WHERE workspace_id = ? AND resolved_path != ''",
        (ws_id,),
    ):
        s, t = r["src_path"], r["resolved_path"]
        if s in meta and t in meta and s != t:
            out_deps[s].add(t)
            rev_deps[t].add(s)

    for r in conn.execute(
        "SELECT DISTINCT caller_path, resolved_path FROM calls WHERE workspace_id = ? AND resolved_path != ''",
        (ws_id,),
    ):
        s, t = r["caller_path"], r["resolved_path"]
        if s in meta and t in meta and s != t:
            out_deps[s].add(t)
            rev_deps[t].add(s)

    return out_deps, rev_deps, meta


# ─── Tarjan Strongly Connected Components (Import Cycle Detection) ───────────


def find_cycles_in_graph(out_deps: dict[str, set[str]]) -> list[list[str]]:
    """Find all non-trivial import/dependency cycles via Tarjan's SCC algorithm."""
    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)

        for w in sorted(out_deps.get(v, ())):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                scc_set = set(scc)
                # Reconstruct an explicit readable cycle path inside the SCC
                start = min(scc)
                cycle_path = _extract_cycle_path(start, out_deps, scc_set)
                cycles.append(cycle_path)

    for node in sorted(out_deps.keys()):
        if node not in indices:
            strongconnect(node)
    return cycles


def _extract_cycle_path(
    start: str, out_deps: dict[str, set[str]], scc_set: set[str]
) -> list[str]:
    queue: deque[tuple[str, list[str]]] = deque([(start, [start])])
    visited: set[str] = set()
    while queue:
        curr, path = queue.popleft()
        for nxt in sorted(out_deps.get(curr, ())):
            if nxt not in scc_set:
                continue
            if nxt == start and len(path) >= 2:
                return path + [start]
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [nxt]))
    return sorted(scc_set) + [start]


# ─── Queries ─────────────────────────────────────────────────────────────────


def get_repo_map(
    root_path: str | Path,
    *,
    scope_prefix: str = "",
    max_files: int = 20,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Return PageRank-ordered architectural map with key symbols and detected cycles."""
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason}
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    prefix = scope_prefix.strip().strip("/")

    with open_locus_db(db_path=db_path) as conn:
        ws = conn.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (ws_id,)
        ).fetchone()
        out_deps, _, meta = _load_dependency_maps(conn, ws_id)
        cycles = find_cycles_in_graph(out_deps)
        drifts = list_open_drift_alerts(conn, ws_id, limit=10)

        candidates = list(meta.values())
        if prefix:
            candidates = [
                f for f in candidates if f["rel_path"] == prefix or f["rel_path"].startswith(prefix + "/")
            ]

        # Rank non-test core modules highest by PageRank, then fan_in
        ranked = sorted(
            candidates,
            key=lambda f: (0 if f["is_test"] else 1, f["pagerank"], f["fan_in"], f["line_count"]),
            reverse=True,
        )[: max(1, max_files)]

        modules: list[dict[str, Any]] = []
        for f in ranked:
            syms = conn.execute(
                """
                SELECT qualified_name, kind, line_start, signature, is_exported
                FROM symbols
                WHERE workspace_id = ? AND rel_path = ? AND is_exported = 1
                ORDER BY CASE kind WHEN 'class' THEN 0 WHEN 'interface' THEN 1 WHEN 'function' THEN 2 WHEN 'async_function' THEN 2 ELSE 3 END, line_start
                LIMIT 8
                """,
                (ws_id, f["rel_path"]),
            ).fetchall()
            modules.append(
                {
                    **f,
                    "top_symbols": [
                        {
                            "name": s["qualified_name"],
                            "kind": s["kind"],
                            "line": int(s["line_start"]),
                            "signature": s["signature"],
                        }
                        for s in syms
                    ],
                }
            )

        lang_counts_rows = conn.execute(
            "SELECT lang, COUNT(*) AS c FROM files WHERE workspace_id = ? GROUP BY lang ORDER BY c DESC",
            (ws_id,),
        ).fetchall()

    return {
        "ok": True,
        "root_path": str(root),
        "file_count": int(ws["file_count"]) if ws else len(meta),
        "symbol_count": int(ws["symbol_count"]) if ws else 0,
        "edge_count": int(ws["edge_count"]) if ws else 0,
        "languages": {r["lang"]: int(r["c"]) for r in lang_counts_rows},
        "modules": modules,
        "cycles": cycles[:10],
        "open_drift_count": len(drifts),
    }


def search_symbols(
    root_path: str | Path,
    query: str,
    *,
    limit: int = 15,
    db_path: Optional[str | Path] = None,
) -> list[dict[str, Any]]:
    """Fuzzy/substring search across workspace symbols ordered by exactness + file PageRank."""
    if not validate_safe_workspace_root(root_path)[0]:
        return []
    _, ws_id = _ensure_indexed(root_path, db_path=db_path)
    q = query.strip()
    with open_locus_db(db_path=db_path) as conn:
        if not q:
            rows = conn.execute(
                """
                SELECT s.*, f.pagerank
                FROM symbols s
                JOIN files f ON f.workspace_id = s.workspace_id AND f.rel_path = s.rel_path
                WHERE s.workspace_id = ? AND s.is_exported = 1
                ORDER BY f.pagerank DESC, s.line_start ASC
                LIMIT ?
                """,
                (ws_id, limit),
            ).fetchall()
        else:
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT s.*, f.pagerank
                FROM symbols s
                JOIN files f ON f.workspace_id = s.workspace_id AND f.rel_path = s.rel_path
                WHERE s.workspace_id = ?
                  AND (s.name LIKE ? OR s.qualified_name LIKE ? OR s.rel_path LIKE ?)
                ORDER BY
                    CASE
                        WHEN LOWER(s.name) = LOWER(?) THEN 0
                        WHEN LOWER(s.qualified_name) = LOWER(?) THEN 1
                        WHEN LOWER(s.name) LIKE LOWER(?) THEN 2
                        ELSE 3
                    END,
                    f.pagerank DESC,
                    s.line_start ASC
                LIMIT ?
                """,
                (ws_id, like, like, like, q, q, f"{q}%", limit),
            ).fetchall()

        return [
            {
                "symbol_id": r["symbol_id"],
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "rel_path": r["rel_path"],
                "line_start": int(r["line_start"]),
                "line_end": int(r["line_end"]),
                "signature": r["signature"],
                "min_args": int(r["min_args"]),
                "max_args": int(r["max_args"]),
                "is_exported": bool(r["is_exported"]),
                "docstring": r["docstring"],
                "pagerank": float(r["pagerank"]),
            }
            for r in rows
        ]


def lookup_symbol_details(
    root_path: str | Path,
    target: str,
    *,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Deep neighborhood lookup for a symbol: definitions, callers, callees, and source snippet."""
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason}
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    matches = search_symbols(root, target, limit=5, db_path=db_path)
    if not matches:
        return {"ok": False, "error": f"No symbol matching '{target}' found in workspace."}

    primary = matches[0]
    sym_name = primary["name"]
    rel_path = primary["rel_path"]

    with open_locus_db(db_path=db_path) as conn:
        caller_rows = conn.execute(
            """
            SELECT caller_path, caller_symbol, line_no, arg_count
            FROM calls
            WHERE workspace_id = ?
              AND callee_name = ?
              AND (resolved_path = ? OR resolved_path = '')
            ORDER BY CASE WHEN caller_path = ? THEN 1 ELSE 0 END, caller_path, line_no
            LIMIT 25
            """,
            (ws_id, sym_name, rel_path, rel_path),
        ).fetchall()

        callee_rows = conn.execute(
            """
            SELECT DISTINCT callee_name, resolved_path, line_no
            FROM calls
            WHERE workspace_id = ?
              AND caller_path = ?
              AND (caller_symbol = ? OR line_no BETWEEN ? AND ?)
            ORDER BY line_no
            LIMIT 25
            """,
            (
                ws_id,
                rel_path,
                primary["qualified_name"],
                primary["line_start"],
                primary["line_end"],
            ),
        ).fetchall()

    snippet = ""
    full_file = root / rel_path
    if not full_file.is_symlink() and full_file.is_file():
        try:
            full_file.resolve().relative_to(root.resolve())
            lines = full_file.read_text(encoding="utf-8", errors="replace").splitlines()
            s_idx = max(0, primary["line_start"] - 1)
            e_idx = min(len(lines), min(primary["line_end"], primary["line_start"] + 24))
            snippet = "\n".join(
                f"{ln_no:4d} | {lines[ln_no - 1]}" for ln_no in range(s_idx + 1, e_idx + 1)
            )
        except (OSError, ValueError):
            snippet = ""

    return {
        "ok": True,
        "symbol": primary,
        "other_matches": matches[1:],
        "callers": [
            {
                "path": r["caller_path"],
                "caller_symbol": r["caller_symbol"],
                "line": int(r["line_no"]),
                "arg_count": int(r["arg_count"]),
            }
            for r in caller_rows
        ],
        "callees": [
            {
                "callee_name": r["callee_name"],
                "resolved_path": r["resolved_path"],
                "line": int(r["line_no"]),
            }
            for r in callee_rows
        ],
        "snippet": snippet,
    }


def analyze_impact(
    root_path: str | Path,
    target: str,
    *,
    max_depth: int = 3,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Compute transitive blast radius, call sites, covering tests, and risk score for *target*."""
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason}
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    clean_target = target.strip().strip("/")

    with open_locus_db(db_path=db_path) as conn:
        _, rev_deps, meta = _load_dependency_maps(conn, ws_id)

        resolved_file = ""
        matched_symbol: Optional[dict[str, Any]] = None

        # 1. Exact or suffix file match
        if clean_target in meta:
            resolved_file = clean_target
        else:
            suffix_hits = [
                p for p in meta if p.endswith("/" + clean_target) or posixpath.basename(p) == clean_target
            ]
            if len(suffix_hits) == 1:
                resolved_file = suffix_hits[0]
            elif len(suffix_hits) > 1:
                resolved_file = sorted(suffix_hits, key=lambda p: meta[p]["pagerank"], reverse=True)[0]

        # 2. Symbol match if not a file path
        if not resolved_file:
            sym_hits = search_symbols(root, clean_target, limit=3, db_path=db_path)
            if sym_hits:
                matched_symbol = sym_hits[0]
                resolved_file = matched_symbol["rel_path"]

        if not resolved_file or resolved_file not in meta:
            return {
                "ok": False,
                "error": f"Target '{target}' matched neither an indexed file nor a symbol.",
            }

        # Reverse BFS up to max_depth
        depths: dict[str, int] = {resolved_file: 0}
        via_parent: dict[str, str] = {}
        queue: deque[str] = deque([resolved_file])
        while queue:
            cur = queue.popleft()
            d = depths[cur]
            if d >= max_depth:
                continue
            for dep in sorted(rev_deps.get(cur, ())):
                if dep not in depths:
                    depths[dep] = d + 1
                    via_parent[dep] = cur
                    queue.append(dep)

        direct_dependents = [
            {**meta[p], "depth": 1, "via": resolved_file}
            for p, d in sorted(depths.items())
            if d == 1 and not meta[p]["is_test"]
        ]
        transitive_dependents = [
            {**meta[p], "depth": d, "via": via_parent.get(p, "")}
            for p, d in sorted(depths.items(), key=lambda kv: (kv[1], kv[0]))
            if d >= 2 and not meta[p]["is_test"]
        ]
        covering_tests = [
            {**meta[p], "depth": d}
            for p, d in sorted(depths.items(), key=lambda kv: (kv[1], kv[0]))
            if d >= 1 and meta[p]["is_test"]
        ]

        # Also catch conventional test files by stem naming even if dynamic/fixture-loaded
        stem = posixpath.splitext(posixpath.basename(resolved_file))[0]
        seen_tests = {t["rel_path"] for t in covering_tests}
        for p, m in meta.items():
            if m["is_test"] and p not in seen_tests and stem in posixpath.basename(p):
                covering_tests.append({**m, "depth": 2})

        # Direct external call sites
        if matched_symbol is not None:
            call_rows = conn.execute(
                """
                SELECT caller_path, caller_symbol, line_no, arg_count
                FROM calls
                WHERE workspace_id = ? AND callee_name = ? AND caller_path != ?
                ORDER BY caller_path, line_no LIMIT 25
                """,
                (ws_id, matched_symbol["name"], resolved_file),
            ).fetchall()
        else:
            call_rows = conn.execute(
                """
                SELECT caller_path, caller_symbol, callee_name, line_no, arg_count
                FROM calls
                WHERE workspace_id = ? AND resolved_path = ? AND caller_path != ?
                ORDER BY caller_path, line_no LIMIT 25
                """,
                (ws_id, resolved_file, resolved_file),
            ).fetchall()

        drifts = [
            d
            for d in list_open_drift_alerts(conn, ws_id, limit=20)
            if d["edited_path"] == resolved_file
        ]

    total_impacted = len(direct_dependents) + len(transitive_dependents)
    untested_penalty = 15 if (total_impacted > 0 and not covering_tests) else 0
    drift_penalty = 25 * len(drifts)
    raw_score = min(
        100,
        len(direct_dependents) * 14
        + len(transitive_dependents) * 6
        + min(20, len(call_rows) * 3)
        + untested_penalty
        + drift_penalty,
    )

    if raw_score >= 65 or drifts:
        risk_level = "CRITICAL" if drifts or raw_score >= 80 else "HIGH"
    elif raw_score >= 30:
        risk_level = "MEDIUM"
    else:
        risk_level = "LOW"

    return {
        "ok": True,
        "target": target,
        "resolved_file": resolved_file,
        "matched_symbol": matched_symbol,
        "file_info": meta[resolved_file],
        "risk_level": risk_level,
        "impact_score": raw_score,
        "direct_dependents": direct_dependents,
        "transitive_dependents": transitive_dependents,
        "covering_tests": covering_tests,
        "external_call_sites": [dict(r) for r in call_rows],
        "open_drift_alerts": drifts,
    }


def get_hotspots_report(
    root_path: str | Path,
    *,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Identify circular dependencies, high-coupling god modules, untested core files & open drift."""
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason}
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    with open_locus_db(db_path=db_path) as conn:
        out_deps, rev_deps, meta = _load_dependency_maps(conn, ws_id)
        cycles = find_cycles_in_graph(out_deps)
        drifts = list_open_drift_alerts(conn, ws_id, limit=20)

        non_tests = [m for m in meta.values() if not m["is_test"]]

        # God modules: high (fan_in + 1) * (fan_out + 1) + line_count weighting
        coupled = sorted(
            non_tests,
            key=lambda m: (m["fan_in"] * 2 + m["fan_out"]) * (1.0 + m["line_count"] / 500.0),
            reverse=True,
        )[:10]

        # Untested high-centrality modules: fan_in >= 1 and no test in 2-hop reverse neighborhood
        untested_core: list[dict[str, Any]] = []
        for m in sorted(non_tests, key=lambda x: (x["pagerank"], x["fan_in"]), reverse=True):
            if m["fan_in"] == 0 and len(non_tests) > 3:
                continue
            p = m["rel_path"]
            r1 = rev_deps.get(p, set())
            r2 = set(r1)
            for d1 in r1:
                r2.update(rev_deps.get(d1, set()))
            has_test = any(meta[cand]["is_test"] for cand in r2 if cand in meta)
            if not has_test:
                untested_core.append(m)
            if len(untested_core) >= 8:
                break

        recent_activity = conn.execute(
            """
            SELECT id, created_at, event_type, target_path, summary, details_json
            FROM activity_log
            WHERE workspace_id = ?
            ORDER BY id DESC LIMIT 15
            """,
            (ws_id,),
        ).fetchall()

    return {
        "ok": True,
        "root_path": str(root),
        "cycles": cycles,
        "god_modules": coupled,
        "untested_core": untested_core,
        "open_drift_alerts": drifts,
        "recent_activity": [
            {
                "id": int(r["id"]),
                "created_at": float(r["created_at"]),
                "event_type": r["event_type"],
                "target_path": r["target_path"],
                "summary": r["summary"],
            }
            for r in recent_activity
        ],
    }


def build_visual_graph(
    root_path: str | Path,
    *,
    focus_target: str = "",
    max_nodes: int = 40,
    db_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Produce a pre-laid-out interactive SVG graph payload for Web Dashboard & Hermes Desktop."""
    is_safe, reason = validate_safe_workspace_root(root_path)
    if not is_safe:
        return {"ok": False, "error": reason, "nodes": [], "edges": []}
    root, ws_id = _ensure_indexed(root_path, db_path=db_path)
    with open_locus_db(db_path=db_path) as conn:
        out_deps, _, meta = _load_dependency_maps(conn, ws_id)
        cycles = find_cycles_in_graph(out_deps)
        drifts = list_open_drift_alerts(conn, ws_id, limit=20)
        recent_rows = conn.execute(
            """
            SELECT target_path, MAX(created_at) AS ts
            FROM activity_log
            WHERE workspace_id = ? AND event_type = 'file_edit' AND target_path != ''
            GROUP BY target_path
            ORDER BY ts DESC LIMIT 10
            """,
            (ws_id,),
        ).fetchall()

    recently_edited = {r["target_path"]: float(r["ts"]) for r in recent_rows}
    drift_by_file = {d["edited_path"]: d for d in drifts}
    cycle_nodes = {n for cyc in cycles for n in cyc}
    cycle_edges: set[tuple[str, str]] = set()
    for cyc in cycles:
        for a, b in zip(cyc, cyc[1:]):
            cycle_edges.add((a, b))

    impact_data: Optional[dict[str, Any]] = None
    blast_depth: dict[str, int] = {}
    if focus_target.strip():
        impact_res = analyze_impact(root, focus_target.strip(), max_depth=3, db_path=db_path)
        if impact_res.get("ok"):
            impact_data = impact_res
            rf = impact_res["resolved_file"]
            blast_depth[rf] = 0
            for item in impact_res["direct_dependents"]:
                blast_depth[item["rel_path"]] = 1
            for item in impact_res["transitive_dependents"]:
                blast_depth[item["rel_path"]] = item["depth"]
            for item in impact_res["covering_tests"]:
                blast_depth[item["rel_path"]] = -1  # covering test marker

    # Choose top nodes ensuring focus neighborhood is always included
    prioritized = sorted(
        meta.keys(),
        key=lambda p: (
            1 if p in blast_depth else 0,
            1 if p in drift_by_file else 0,
            1 if p in recently_edited else 0,
            meta[p]["pagerank"],
            meta[p]["fan_in"] + meta[p]["fan_out"],
        ),
        reverse=True,
    )[: max(8, max_nodes)]
    selected_set = set(prioritized)

    # Radial/concentric layout deterministic coordinates on an 880 x 520 canvas
    cx, cy = 440.0, 260.0
    n_total = max(1, len(prioritized))
    nodes_payload: list[dict[str, Any]] = []

    if impact_data and impact_data["resolved_file"] in selected_set:
        center_file = impact_data["resolved_file"]
        ring1 = [p for p in prioritized if blast_depth.get(p) == 1]
        ring2 = [p for p in prioritized if (blast_depth.get(p, 99) >= 2 and blast_depth.get(p, 99) < 90) or blast_depth.get(p) == -1]
        outer = [p for p in prioritized if p != center_file and p not in ring1 and p not in ring2]

        coords: dict[str, tuple[float, float]] = {center_file: (cx, cy)}
        for ring_nodes, rx, ry, phase in (
            (ring1, 165.0, 120.0, 0.2),
            (ring2, 285.0, 185.0, 0.7),
            (outer, 385.0, 225.0, 1.2),
        ):
            cnt = len(ring_nodes)
            for idx, p in enumerate(ring_nodes):
                angle = phase + (2.0 * math.pi * idx) / max(1, cnt)
                coords[p] = (round(cx + rx * math.cos(angle), 1), round(cy + ry * math.sin(angle), 1))
    else:
        # Place core highest-PageRank hubs on inner orbit, peripheral/tests on outer orbit
        inner = prioritized[: min(8, max(1, n_total // 3))]
        outer = prioritized[len(inner) :]
        coords = {}
        for idx, p in enumerate(inner):
            angle = -math.pi / 2 + (2.0 * math.pi * idx) / max(1, len(inner))
            r_x = 135.0 if len(inner) > 1 else 0.0
            r_y = 95.0 if len(inner) > 1 else 0.0
            coords[p] = (round(cx + r_x * math.cos(angle), 1), round(cy + r_y * math.sin(angle), 1))
        for idx, p in enumerate(outer):
            angle = 0.35 + (2.0 * math.pi * idx) / max(1, len(outer))
            coords[p] = (round(cx + 325.0 * math.cos(angle), 1), round(cy + 195.0 * math.sin(angle), 1))

    max_pr = max((meta[p]["pagerank"] for p in prioritized), default=0.01) or 0.01

    for p in prioritized:
        m = meta[p]
        x, y = coords.get(p, (cx, cy))
        role = "normal"
        if p in blast_depth:
            bd = blast_depth[p]
            if bd == 0:
                role = "focus"
            elif bd == 1:
                role = "direct_dependent"
            elif bd == -1:
                role = "covering_test"
            else:
                role = "transitive_dependent"
        elif m["is_test"]:
            role = "test"

        nodes_payload.append(
            {
                "id": p,
                "label": posixpath.basename(p),
                "folder": posixpath.dirname(p) or ".",
                "lang": m["lang"],
                "lines": m["line_count"],
                "pagerank": m["pagerank"],
                "weight": round(m["pagerank"] / max_pr, 3),
                "fan_in": m["fan_in"],
                "fan_out": m["fan_out"],
                "is_test": m["is_test"],
                "in_cycle": p in cycle_nodes,
                "has_drift": p in drift_by_file,
                "recently_edited": p in recently_edited,
                "role": role,
                "x": x,
                "y": y,
            }
        )

    edges_payload: list[dict[str, Any]] = []
    for src in prioritized:
        for tgt in sorted(out_deps.get(src, ())):
            if tgt not in selected_set:
                continue
            edge_kind = "normal"
            if (src, tgt) in cycle_edges:
                edge_kind = "cycle"
            elif src in blast_depth and tgt in blast_depth and blast_depth[tgt] >= 0:
                edge_kind = "blast"
            edges_payload.append(
                {
                    "source": src,
                    "target": tgt,
                    "kind": edge_kind,
                }
            )

    return {
        "ok": True,
        "root_path": str(root),
        "total_files": len(meta),
        "shown_files": len(nodes_payload),
        "nodes": nodes_payload,
        "edges": edges_payload,
        "cycles": cycles[:10],
        "drift_alerts": drifts,
        "focus_impact": impact_data,
    }
