"""Shared FastAPI backend for Locus Codegraph Web Dashboard and Native Hermes Desktop (/api/plugins/locus-codegraph/*).

All synchronous AST/SQLite operations execute off the gateway event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

_PLUGIN_ROOT = str(Path(__file__).resolve().parent.parent)
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)

from locus_core import (  # noqa: E402
    analyze_impact,
    build_visual_graph,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
    lookup_symbol_details,
    open_locus_db,
    search_symbols,
    validate_safe_workspace_root,
)

router = APIRouter()


def _list_known_workspaces() -> list[dict[str, Any]]:
    with open_locus_db() as conn:
        rows = conn.execute(
            """
            SELECT workspace_id, root_path, last_indexed_at, file_count, symbol_count, edge_count, scan_ms
            FROM workspaces
            ORDER BY last_indexed_at DESC
            LIMIT 15
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        p = Path(r["root_path"])
        if p.is_dir():
            out.append(
                {
                    "workspace_id": r["workspace_id"],
                    "root_path": r["root_path"],
                    "name": p.name or r["root_path"],
                    "last_indexed_at": float(r["last_indexed_at"]),
                    "file_count": int(r["file_count"]),
                    "symbol_count": int(r["symbol_count"]),
                    "edge_count": int(r["edge_count"]),
                    "scan_ms": float(r["scan_ms"]),
                }
            )
    return out


def _resolve_api_workspace(explicit: str = "") -> Path:
    """Resolve target workspace: explicit query -> most recent indexed workspace -> runtime cwd."""
    if explicit and explicit.strip():
        raw = Path(explicit.strip()).expanduser()
        if not raw.is_symlink() and not raw.is_dir():
            raise HTTPException(status_code=400, detail=f"Workspace directory not found: {explicit}")
        is_safe, reason = validate_safe_workspace_root(raw)
        if not is_safe:
            raise HTTPException(status_code=403, detail=reason)
        return raw.resolve()

    for item in _list_known_workspaces():
        cand = Path(item["root_path"])
        if validate_safe_workspace_root(cand)[0]:
            return cand.resolve()

    for env_key in ("LOCUS_WORKSPACE", "TERMINAL_CWD"):
        val = os.environ.get(env_key, "").strip()
        if val:
            cand = Path(val).expanduser()
            if validate_safe_workspace_root(cand)[0]:
                return cand.resolve()

    try:
        from agent.runtime_cwd import resolve_agent_cwd

        rcwd = Path(resolve_agent_cwd()).expanduser()
        if validate_safe_workspace_root(rcwd)[0]:
            return rcwd.resolve()
    except Exception:
        pass

    cwd = Path.cwd()
    if validate_safe_workspace_root(cwd)[0]:
        return cwd.resolve()
    return Path(_PLUGIN_ROOT).resolve()


def _notify_desktop(root: Path, reason: str) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event("locus-codegraph", "graph.updated", {"workspace": str(root), "reason": reason})
    except (Exception, SystemExit):
        pass


def _build_summary_sync(workspace: str) -> dict[str, Any]:
    root = _resolve_api_workspace(workspace)
    repo = get_repo_map(root, max_files=12)
    hotspots = get_hotspots_report(root)
    workspaces = _list_known_workspaces()
    return {
        "ok": True,
        "workspace": str(root),
        "workspace_name": root.name,
        "workspaces": workspaces,
        "file_count": repo.get("file_count", 0),
        "symbol_count": repo.get("symbol_count", 0),
        "edge_count": repo.get("edge_count", 0),
        "languages": repo.get("languages", {}),
        "cycle_count": len(hotspots.get("cycles") or []),
        "cycles": (hotspots.get("cycles") or [])[:8],
        "open_drift_count": len(hotspots.get("open_drift_alerts") or []),
        "drift_alerts": hotspots.get("open_drift_alerts") or [],
        "top_modules": repo.get("modules") or [],
        "untested_core": hotspots.get("untested_core") or [],
        "recent_activity": hotspots.get("recent_activity") or [],
    }


@router.get("/summary")
async def api_summary(workspace: str = Query(default="")) -> dict[str, Any]:
    return await asyncio.to_thread(_build_summary_sync, workspace)


@router.get("/graph")
async def api_graph(
    workspace: str = Query(default=""),
    focus: str = Query(default=""),
    max_nodes: int = Query(default=36, ge=8, le=80),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(workspace)
        return build_visual_graph(root, focus_target=focus, max_nodes=max_nodes)

    return await asyncio.to_thread(_run)


@router.get("/impact")
async def api_impact(
    target: str = Query(..., min_length=1),
    workspace: str = Query(default=""),
    max_depth: int = Query(default=3, ge=1, le=5),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(workspace)
        return analyze_impact(root, target, max_depth=max_depth)

    return await asyncio.to_thread(_run)


@router.get("/symbol")
async def api_symbol(
    name: str = Query(..., min_length=1),
    workspace: str = Query(default=""),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(workspace)
        return lookup_symbol_details(root, name)

    return await asyncio.to_thread(_run)


@router.get("/symbols")
async def api_symbols(
    q: str = Query(default=""),
    workspace: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(workspace)
        items = search_symbols(root, q, limit=limit)
        return {"ok": True, "workspace": str(root), "symbols": items}

    return await asyncio.to_thread(_run)


@router.get("/hotspots")
async def api_hotspots(workspace: str = Query(default="")) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(workspace)
        return get_hotspots_report(root)

    return await asyncio.to_thread(_run)


@router.post("/reindex")
async def api_reindex(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = body or {}
    ws_arg = str(payload.get("workspace") or "")
    force = bool(payload.get("force", True))

    def _run() -> dict[str, Any]:
        root = _resolve_api_workspace(ws_arg)
        res = index_workspace(root, force=force)
        _notify_desktop(root, "dashboard_reindex")
        return res

    return await asyncio.to_thread(_run)


@router.post("/resolve-drift")
async def api_resolve_drift(body: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    payload = body or {}
    alert_id = int(payload.get("alert_id") or 0)
    if alert_id <= 0:
        raise HTTPException(status_code=400, detail="Valid alert_id is required")

    def _run() -> dict[str, Any]:
        with open_locus_db() as conn:
            conn.execute("UPDATE drift_alerts SET resolved = 1 WHERE id = ?", (alert_id,))
        return {"ok": True, "resolved_id": alert_id}

    return await asyncio.to_thread(_run)
