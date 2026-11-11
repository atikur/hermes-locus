"""Locus Codegraph — codebase graph, blast radius, and signature drift feedback for Hermes Agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from .locus_core import (
    analyze_impact,
    format_drift_banner,
    format_hotspots_markdown,
    format_impact_markdown,
    format_repo_map_markdown,
    format_symbol_markdown,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
    lookup_symbol_details,
    list_open_drift_alerts,
    merge_drift_into_tool_result,
    open_locus_db,
    reindex_edited_file,
    search_symbols,
    validate_safe_workspace_root,
    workspace_id_for,
)
from .locus_core.storage import PLUGIN_NAME

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext

try:
    from agent.context_references import ContextCompletionItem, ContextReferenceProvider
except Exception:  # pragma: no cover - fallback when imported outside a full Hermes checkout
    from dataclasses import dataclass

    @dataclass(slots=True)
    class ContextCompletionItem:  # type: ignore[no-redef]
        text: str
        display: str = ""
        meta: str = ""

    class ContextReferenceProvider:  # type: ignore[no-redef]
        prefix: str = ""
        description: str = ""

logger = logging.getLogger(__name__)

_FILE_MUTATION_TOOLS = frozenset(
    {
        "write_file",
        "patch",
        "edit_file",
        "apply_patch",
        "multi_edit",
        "replace_in_file",
        "create_file",
    }
)
_PATH_ARG_KEYS = ("path", "file_path", "filepath", "target_file", "filename")

_MAX_PENDING_DRIFT = 64


# ─── Tool Schemas ────────────────────────────────────────────────────────────

LOCUS_MAP_SCHEMA: dict[str, Any] = {
    "name": "locus_map",
    "description": (
        "Return a PageRank-ranked architectural map of the workspace showing central hub modules, "
        "exported class/function signatures, language breakdown, circular import chains, and open "
        "signature drift alerts. Call this first when orienting on a codebase or planning changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope_prefix": {
                "type": "string",
                "description": "Optional relative directory/file prefix to scope the map (e.g. 'src/auth').",
            },
            "max_files": {
                "type": "integer",
                "description": "Maximum number of ranked modules to include (default: 15, max: 50).",
            },
            "workspace": {
                "type": "string",
                "description": "Optional workspace root path override (defaults to active session cwd).",
            },
        },
        "required": [],
    },
}

LOCUS_SYMBOL_SCHEMA: dict[str, Any] = {
    "name": "locus_symbol",
    "description": (
        "Look up a function, class, method, or type across the workspace AST graph. Returns its exact "
        "definition location, signature, incoming workspace call sites with argument counts, outgoing "
        "callees, and a bounded source snippet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Symbol or qualified name to inspect (e.g. 'verify_token' or 'AuthService.login').",
            },
            "workspace": {
                "type": "string",
                "description": "Optional workspace root path override.",
            },
        },
        "required": ["name"],
    },
}

LOCUS_IMPACT_SCHEMA: dict[str, Any] = {
    "name": "locus_impact",
    "description": (
        "Compute the pre-refactor blast radius of editing a file or symbol: direct dependents (hop 1), "
        "transitive dependents (hops 2-3), external call sites, covering test files to run, open "
        "signature drift alerts, and a 0-100 risk score (LOW / MEDIUM / HIGH / CRITICAL)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Relative file path (e.g. 'src/auth/tokens.py') or symbol name (e.g. 'verify_token').",
            },
            "max_depth": {
                "type": "integer",
                "description": "Reverse dependency traversal depth from 1 to 5 (default: 3).",
            },
            "workspace": {
                "type": "string",
                "description": "Optional workspace root path override.",
            },
        },
        "required": ["target"],
    },
}

LOCUS_HOTSPOTS_SCHEMA: dict[str, Any] = {
    "name": "locus_hotspots",
    "description": (
        "Audit codebase architectural health: strongly connected circular import cycles (Tarjan SCC), "
        "high-coupling hub/god modules, high-centrality modules lacking test coverage, and unresolved "
        "signature drift alerts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace": {
                "type": "string",
                "description": "Optional workspace root path override.",
            },
        },
        "required": [],
    },
}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_workspace_root(ctx: Any = None, explicit: Optional[str] = None) -> Path:
    """Resolve the active session's workspace root safely without touching gateway internals."""
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip()).expanduser()

    if ctx is not None:
        try:
            scwd = ctx.session_cwd()
            if isinstance(scwd, str) and scwd.strip():
                cand = Path(scwd).expanduser()
                if validate_safe_workspace_root(cand)[0]:
                    return cand.resolve()
        except Exception:
            pass

    try:
        from agent.runtime_cwd import resolve_agent_cwd

        rcwd = resolve_agent_cwd()
        if isinstance(rcwd, (str, Path)) and str(rcwd).strip():
            cand = Path(rcwd).expanduser()
            if validate_safe_workspace_root(cand)[0]:
                return cand.resolve()
    except Exception:
        pass

    for env_key in ("LOCUS_WORKSPACE", "TERMINAL_CWD"):
        val = os.environ.get(env_key, "").strip()
        if val:
            cand = Path(val).expanduser()
            if validate_safe_workspace_root(cand)[0]:
                return cand.resolve()

    return Path.cwd()


def _record_active_workspace(ctx: Any, root: Path) -> None:
    if ctx is None:
        return
    try:
        ctx.state.set("last_workspace", str(root))
    except Exception:
        pass


def _broadcast_desktop_update(event_name: str, payload: dict[str, Any]) -> None:
    """Push real-time graph updates to connected Hermes Desktop panes (no-op if offline)."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event(PLUGIN_NAME, event_name, payload)
    except (Exception, SystemExit):
        pass


def _call_drift_key(root: Path, tool_name: str, args: dict[str, Any], task_id: Optional[str], kwargs: dict[str, Any]) -> str:
    # Some providers reuse call IDs. Scope them to their owner and turn as well.
    identity = [str(root.resolve()), task_id, kwargs.get("session_id"), kwargs.get("turn_id"),
                kwargs.get("api_request_id"), kwargs.get("tool_call_id"), tool_name, args]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def _result_object(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def _edited_paths(args: dict[str, Any], result: Any, *, result_only: bool = False) -> list[str]:
    """Read actual Hermes write/patch paths, including V4A multi-file operations."""
    paths: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in paths:
            paths.append(value.strip())

    payload = _result_object(result)
    add(payload.get("resolved_path"))
    for field in ("files_modified", "files_created", "files_deleted"):
        for value in payload.get(field, []) if isinstance(payload.get(field), list) else []:
            # A partially applied Hermes Move operation reports "old -> new".
            if isinstance(value, str) and " -> " in value:
                for endpoint in value.split(" -> ", 1):
                    add(endpoint)
            else:
                add(value)
    if paths or result_only:
        return paths

    for field in _PATH_ARG_KEYS:
        add(args.get(field))
    patch = args.get("patch")
    if isinstance(patch, str):
        for line in patch.splitlines():
            match = re.match(r"^\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)$", line)
            if match:
                add(match.group(1))
            move = re.match(r"^\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)$", line)
            if move:
                add(move.group(1))
                add(move.group(2))
            elif line.startswith("*** Move to:"):
                add(line.partition(":")[2])
    return paths


def _tool_result_failed(result: Any, kwargs: dict[str, Any]) -> bool:
    if kwargs.get("status") in {"blocked", "error", "cancelled", "timeout"}:
        return True
    parsed = _result_object(result)
    return bool(parsed.get("error")) or parsed.get("success") is False or parsed.get("ok") is False


def _append_index_warnings(result: Any, warnings: list[dict[str, str]]) -> str:
    """Keep parse diagnostics separate from caller-contract drift in host results."""
    shown = warnings[:5]
    lines = ["[Locus Indexing Warning]", "These edited files could not be indexed:"]
    lines.extend(f"- `{item['path']}`: {item['error']}" for item in shown)
    if len(warnings) > len(shown):
        lines.append(f"- {len(warnings) - len(shown)} more file(s) could not be indexed.")
    lines.append("Graph results may reflect the last valid version. Fix the syntax and re-index before relying on them.")
    banner = "\n".join(lines)
    payload: Any = result
    if isinstance(result, str):
        try:
            payload = json.loads(result)
        except ValueError:
            payload = None
    if isinstance(payload, dict):
        payload = dict(payload)
        payload["locus_index_warnings"] = shown
        for key in ("output", "message", "result", "detail"):
            if isinstance(payload.get(key), str):
                payload[key] += "\n\n" + banner
                break
        else:
            payload["locus_index_warning"] = banner
        return json.dumps(payload, ensure_ascii=False)
    return f"{result}\n\n{banner}"


# ─── @ContextReferenceProviders (@sym:, @impact:, @hotspots:) ────────────────


class LocusSymbolReferenceProvider(ContextReferenceProvider):
    """Inline AST symbol definitions, callers, and snippets via ``@sym:<query>``."""

    prefix = "sym"
    description = "Inline AST symbol signature, callers & source excerpt (Locus Codegraph)"

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        root = _resolve_workspace_root(self._ctx)
        hits = await asyncio.to_thread(search_symbols, root, query, limit=max(1, min(limit, 20)))
        return [
            ContextCompletionItem(
                text=h["qualified_name"],
                display=f"{h['qualified_name']} ({h['kind']})",
                meta=f"{h['rel_path']}:{h['line_start']}",
            )
            for h in hits
        ]

    async def expand(self, target: str) -> str | None:
        clean = (target or "").strip()
        if not clean:
            return None
        root = _resolve_workspace_root(self._ctx)
        details = await asyncio.to_thread(lookup_symbol_details, root, clean)
        if not details.get("ok"):
            return None
        return format_symbol_markdown(details)


class LocusImpactReferenceProvider(ContextReferenceProvider):
    """Inline transitive blast-radius & covering test report via ``@impact:<file-or-symbol>``."""

    prefix = "impact"
    description = "Inline transitive blast radius, callers & suggested tests (Locus Codegraph)"

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        root = _resolve_workspace_root(self._ctx)
        q = (query or "").strip().lower()
        repo = await asyncio.to_thread(get_repo_map, root, max_files=40)
        items: list[ContextCompletionItem] = []
        for mod in repo.get("modules", []):
            rel = mod["rel_path"]
            if not q or q in rel.lower():
                items.append(
                    ContextCompletionItem(
                        text=rel,
                        display=rel,
                        meta=f"fan-in {mod['fan_in']} · {mod['line_count']}L",
                    )
                )
            if len(items) >= limit:
                return items[:limit]

        sym_hits = await asyncio.to_thread(search_symbols, root, query, limit=max(1, limit - len(items)))
        for s in sym_hits:
            items.append(
                ContextCompletionItem(
                    text=s["qualified_name"],
                    display=f"{s['qualified_name']} (symbol)",
                    meta=f"{s['rel_path']}:{s['line_start']}",
                )
            )
        return items[:limit]

    async def expand(self, target: str) -> str | None:
        clean = (target or "").strip()
        if not clean:
            return None
        root = _resolve_workspace_root(self._ctx)
        impact = await asyncio.to_thread(analyze_impact, root, clean, max_depth=3)
        if not impact.get("ok"):
            return None
        return format_impact_markdown(impact)


class LocusHotspotsReferenceProvider(ContextReferenceProvider):
    """Inline architectural cycles, god-modules & drift report via ``@hotspots:all``."""

    prefix = "hotspots"
    description = "Inline circular dependencies, hub modules & drift alerts (Locus Codegraph)"

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def autocomplete(self, query: str, *, limit: int = 10) -> list[ContextCompletionItem]:
        options = [
            ContextCompletionItem("all", "all (full health audit)", "Cycles + coupling + untested + drift"),
            ContextCompletionItem("cycles", "cycles", "Circular dependency chains only"),
            ContextCompletionItem("drift", "drift", "Open signature drift alerts only"),
            ContextCompletionItem("untested", "untested", "High-centrality modules missing tests"),
        ]
        q = (query or "").strip().lower()
        return [o for o in options if not q or q in o.text][:limit]

    async def expand(self, target: str) -> str | None:
        mode = (target or "all").strip().lower() or "all"
        root = _resolve_workspace_root(self._ctx)
        report = await asyncio.to_thread(get_hotspots_report, root)
        if not report.get("ok"):
            return None
        if mode == "cycles":
            report = {**report, "god_modules": [], "untested_core": [], "open_drift_alerts": []}
        elif mode == "drift":
            report = {**report, "cycles": [], "god_modules": [], "untested_core": []}
        elif mode == "untested":
            report = {**report, "cycles": [], "god_modules": [], "open_drift_alerts": []}
        return format_hotspots_markdown(report)


# ─── Slash Command & CLI Subcommand ──────────────────────────────────────────


def _handle_slash_command(ctx: Any, raw_args: str = "", **kwargs: Any) -> str:
    tokens = (raw_args or "").strip().split(maxsplit=1)
    sub = tokens[0].lower() if tokens else ""
    rest = tokens[1].strip() if len(tokens) > 1 else ""

    if not sub:
        try:
            picked = ctx.ask_choice(
                "Locus Codegraph — choose an architectural view:",
                [
                    {"value": "status", "label": "Workspace Graph Summary", "description": "Files, symbols, edges, cycles & drift"},
                    {"value": "map", "label": "PageRank Architectural Map", "description": "Top central modules & exported signatures"},
                    {"value": "hotspots", "label": "Hotspots & Health Audit", "description": "Import cycles, god modules & untested core"},
                    {"value": "reindex", "label": "Force Full Re-Index", "description": "Rescan all workspace source files"},
                ],
            )
            sub = picked or "status"
        except Exception:
            sub = "status"

    root = _resolve_workspace_root(ctx)
    _record_active_workspace(ctx, root)

    if sub in ("reindex", "scan"):
        max_files = int(ctx.get_config("max_files", 2500) or 2500)
        stats = index_workspace(root, force=True, max_files=max_files)
        _broadcast_desktop_update("graph.updated", {"workspace": str(root), "reason": "manual_reindex"})
        return (
            f"✅ **Locus Codegraph Re-Indexed** `{stats.get('root_path', root)}`\n"
            f"- Files: `{stats.get('file_count', 0)}` · Symbols: `{stats.get('symbol_count', 0)}` · "
            f"Edges: `{stats.get('edge_count', 0)}` · Time: `{stats.get('scan_ms', 0)} ms`"
        )

    if sub == "map":
        return format_repo_map_markdown(get_repo_map(root, scope_prefix=rest, max_files=15))

    if sub == "impact":
        if not rest:
            return "Usage: `/locus impact <file-path-or-symbol>` (e.g. `/locus impact storage.py`)"
        return format_impact_markdown(analyze_impact(root, rest, max_depth=3))

    if sub in ("symbol", "sym"):
        if not rest:
            return "Usage: `/locus symbol <name>` (e.g. `/locus symbol analyze_impact`)"
        return format_symbol_markdown(lookup_symbol_details(root, rest))

    if sub in ("hotspots", "cycles", "drift"):
        rep = get_hotspots_report(root)
        if sub == "cycles":
            rep = {**rep, "god_modules": [], "untested_core": [], "open_drift_alerts": []}
        elif sub == "drift":
            rep = {**rep, "cycles": [], "god_modules": [], "untested_core": []}
        return format_hotspots_markdown(rep)

    # Default: status overview
    repo = get_repo_map(root, max_files=5)
    return (
        f"### Locus Codegraph Status (`{root.name}`)\n"
        f"- **Workspace**: `{repo.get('root_path', root)}`\n"
        f"- **Indexed**: `{repo.get('file_count', 0)}` files · `{repo.get('symbol_count', 0)}` symbols · "
        f"`{repo.get('edge_count', 0)}` edges\n"
        f"- **Import Cycles**: `{len(repo.get('cycles', []))}` · "
        f"**Open Drift Alerts**: `{repo.get('open_drift_count', 0)}`\n"
        f"- **Try**: `/locus map`, `/locus impact <target>`, `/locus symbol <name>`, `/locus hotspots`, "
        f"or type `@sym:`, `@impact:`, `@hotspots:` inline in chat!"
    )


def _setup_cli_parser(parser: Any) -> None:
    parser.add_argument(
        "action",
        nargs="?",
        default="map",
        choices=["scan", "map", "impact", "symbol", "hotspots"],
        help="Graph command to run (default: map)",
    )
    parser.add_argument("target", nargs="?", default="", help="Target file/symbol/scope for impact, symbol, or map")
    parser.add_argument("--workspace", "-w", default="", help="Workspace directory (defaults to current directory)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON output")


def _handle_cli_command(args: Any) -> int:
    root = _resolve_workspace_root(None, getattr(args, "workspace", ""))
    action = getattr(args, "action", "map")
    target = getattr(args, "target", "")
    as_json = bool(getattr(args, "as_json", False))

    if action == "scan":
        data = index_workspace(root, force=True)
        print(json.dumps(data, indent=2) if as_json else f"Indexed {data.get('file_count', 0)} files, {data.get('symbol_count', 0)} symbols, {data.get('edge_count', 0)} edges in {data.get('scan_ms', 0)}ms.")
        return 0 if data.get("ok") else 1

    if action == "impact":
        if not target:
            print("Error: 'hermes locus impact <target>' requires a file or symbol target.")
            return 2
        data = analyze_impact(root, target)
        print(json.dumps(data, indent=2) if as_json else format_impact_markdown(data))
        return 0 if data.get("ok") else 1

    if action == "symbol":
        if not target:
            print("Error: 'hermes locus symbol <name>' requires a symbol name.")
            return 2
        data = lookup_symbol_details(root, target)
        print(json.dumps(data, indent=2) if as_json else format_symbol_markdown(data))
        return 0 if data.get("ok") else 1

    if action == "hotspots":
        data = get_hotspots_report(root)
        print(json.dumps(data, indent=2) if as_json else format_hotspots_markdown(data))
        return 0 if data.get("ok") else 1

    data = get_repo_map(root, scope_prefix=target, max_files=15)
    print(json.dumps(data, indent=2) if as_json else format_repo_map_markdown(data))
    return 0 if data.get("ok") else 1


# ─── Plugin Entry Point ──────────────────────────────────────────────────────


def register(ctx: PluginContext) -> None:
    """Register Locus Codegraph tools, context references, hooks, commands, and skill."""
    drift_lock = threading.Lock()
    # Both hook orders occur in Hermes. Retain bounded, per-registration receipts
    # so inspection happens once and only transform_tool_result delivers the alert.
    inspected_calls: dict[str, tuple[str, list[dict[str, str]], bool]] = {}

    # 1. Model Tools (return JSON strings per Hermes tool contract)
    def handle_locus_map(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            root = _resolve_workspace_root(ctx, args.get("workspace"))
            _record_active_workspace(ctx, root)
            max_f = max(1, min(int(args.get("max_files") or 15), 50))
            data = get_repo_map(root, scope_prefix=str(args.get("scope_prefix") or ""), max_files=max_f)
            return json.dumps(
                {
                    "ok": data.get("ok", False),
                    "report": format_repo_map_markdown(data),
                    "summary": {
                        "file_count": data.get("file_count", 0),
                        "symbol_count": data.get("symbol_count", 0),
                        "edge_count": data.get("edge_count", 0),
                        "cycle_count": len(data.get("cycles") or []),
                        "open_drift_count": data.get("open_drift_count", 0),
                    },
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"locus_map failed: {exc}"})

    def handle_locus_symbol(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            name = str(args.get("name") or "").strip()
            if not name:
                return json.dumps({"ok": False, "error": "Parameter 'name' is required."})
            root = _resolve_workspace_root(ctx, args.get("workspace"))
            _record_active_workspace(ctx, root)
            data = lookup_symbol_details(root, name)
            if not data.get("ok"):
                return json.dumps(data, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": True,
                    "report": format_symbol_markdown(data),
                    "symbol": data["symbol"],
                    "caller_count": len(data.get("callers") or []),
                    "callee_count": len(data.get("callees") or []),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"locus_symbol failed: {exc}"})

    def handle_locus_impact(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            target = str(args.get("target") or "").strip()
            if not target:
                return json.dumps({"ok": False, "error": "Parameter 'target' is required."})
            depth = max(1, min(int(args.get("max_depth") or 3), 5))
            root = _resolve_workspace_root(ctx, args.get("workspace"))
            _record_active_workspace(ctx, root)
            data = analyze_impact(root, target, max_depth=depth)
            if not data.get("ok"):
                return json.dumps(data, ensure_ascii=False)
            return json.dumps(
                {
                    "ok": True,
                    "report": format_impact_markdown(data),
                    "resolved_file": data["resolved_file"],
                    "risk_level": data["risk_level"],
                    "impact_score": data["impact_score"],
                    "direct_dependents": [d["rel_path"] for d in data["direct_dependents"]],
                    "transitive_dependents": [d["rel_path"] for d in data["transitive_dependents"]],
                    "covering_tests": [t["rel_path"] for t in data["covering_tests"]],
                    "open_drift_alerts": data["open_drift_alerts"],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"locus_impact failed: {exc}"})

    def handle_locus_hotspots(args: dict[str, Any], **kwargs: Any) -> str:
        try:
            root = _resolve_workspace_root(ctx, args.get("workspace"))
            _record_active_workspace(ctx, root)
            data = get_hotspots_report(root)
            return json.dumps(
                {
                    "ok": data.get("ok", False),
                    "report": format_hotspots_markdown(data),
                    "cycle_count": len(data.get("cycles") or []),
                    "open_drift_count": len(data.get("open_drift_alerts") or []),
                    "untested_core_count": len(data.get("untested_core") or []),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": f"locus_hotspots failed: {exc}"})

    ctx.register_tool(
        name="locus_map",
        toolset=PLUGIN_NAME,
        schema=LOCUS_MAP_SCHEMA,
        handler=handle_locus_map,
        description="PageRank architectural map of central workspace modules, signatures & cycles",
        emoji="🗺️",
    )
    ctx.register_tool(
        name="locus_symbol",
        toolset=PLUGIN_NAME,
        schema=LOCUS_SYMBOL_SCHEMA,
        handler=handle_locus_symbol,
        description="AST symbol definition, callers, callees & source snippet lookup",
        emoji="🔎",
    )
    ctx.register_tool(
        name="locus_impact",
        toolset=PLUGIN_NAME,
        schema=LOCUS_IMPACT_SCHEMA,
        handler=handle_locus_impact,
        description="Pre-refactor transitive blast radius, external callers & covering tests",
        emoji="💥",
    )
    ctx.register_tool(
        name="locus_hotspots",
        toolset=PLUGIN_NAME,
        schema=LOCUS_HOTSPOTS_SCHEMA,
        handler=handle_locus_hotspots,
        description="Detect circular dependencies, god modules, untested core files & drift",
        emoji="🩺",
    )

    # 2. Lifecycle Hooks (all accept **kwargs, bounded & fail-open)
    def on_session_start(session_id: str = "", **kwargs: Any) -> None:
        if not bool(ctx.get_config("auto_index_on_start", True)):
            return
        try:
            root = _resolve_workspace_root(ctx)
            _record_active_workspace(ctx, root)
            max_files = int(ctx.get_config("max_files", 2500) or 2500)
            index_workspace(root, force=False, max_files=max_files)
        except Exception as exc:
            logger.debug("Locus auto-index skipped on session start: %s", exc)

    def inspect_tool_edit(
        tool_name: str = "",
        args: Optional[dict[str, Any]] = None,
        result: Any = None,
        task_id: Optional[str] = None,
        *,
        deliver: bool = False,
        **kwargs: Any,
    ) -> Optional[str]:
        if tool_name not in _FILE_MUTATION_TOOLS or not isinstance(args, dict):
            return None
        failed = _tool_result_failed(result, kwargs)
        if kwargs.get("status") in {"blocked", "cancelled", "timeout"}:
            return None
        # V4A can partially apply before an error. Only inspect paths the result
        # confirms were changed in that case, never merely intended arguments.
        edited_paths = _edited_paths(args, result, result_only=failed)
        if not edited_paths:
            return None
        try:
            root = _resolve_workspace_root(ctx)
            key = _call_drift_key(root, tool_name, args, task_id, kwargs)
            with drift_lock:
                if key not in inspected_calls:
                    _record_active_workspace(ctx, root)
                    candidates: set[tuple[str, str]] = set()
                    changed: list[str] = []
                    index_warnings: list[dict[str, str]] = []
                    for edited_path in edited_paths:
                        outcome = reindex_edited_file(root, edited_path, tool_name=tool_name)
                        if outcome.get("reason") == "parse_error":
                            index_warnings.append({
                                "path": outcome.get("rel_path", edited_path),
                                "error": str(outcome.get("error") or "Source could not be parsed"),
                            })
                        if outcome.get("ok") and not outcome.get("skipped"):
                            changed.append(outcome.get("rel_path", edited_path))
                            candidates.update((a["edited_path"], a["symbol_name"])
                                              for a in outcome.get("drift_alerts") or [])
                    # A single patch may change both a definition and its callers.
                    # Report only alerts still open after every changed file is indexed.
                    alerts: list[dict[str, Any]] = []
                    if candidates:
                        with open_locus_db() as conn:
                            alerts = [a for a in list_open_drift_alerts(conn, workspace_id_for(root), limit=-1)
                                      if (a["edited_path"], a["symbol_name"]) in candidates]
                    banner = format_drift_banner(alerts) if bool(ctx.get_config("drift_guard", True)) else ""
                    if len(inspected_calls) >= _MAX_PENDING_DRIFT:
                        inspected_calls.pop(next(iter(inspected_calls)))
                    inspected_calls[key] = (banner, index_warnings, False)
                    if changed:
                        _broadcast_desktop_update("graph.updated", {
                            "workspace": str(root), "edited_paths": changed, "drift_count": len(alerts),
                        })
                banner, index_warnings, delivered = inspected_calls[key]
                if not deliver or delivered:
                    return None
                if kwargs.get("tool_call_id"):
                    inspected_calls[key] = (banner, index_warnings, True)
                else:
                    # execute_code calls can omit IDs; don't suppress a later edit
                    # just because it reuses the same arguments.
                    inspected_calls.pop(key, None)
                transformed = merge_drift_into_tool_result(result, banner) if banner else result
                if index_warnings:
                    return _append_index_warnings(transformed, index_warnings)
                return transformed if banner else None
        except Exception as exc:
            logger.debug("Locus edit inspection skipped: %s", exc)
            return None

    def on_post_tool_call(**kwargs: Any) -> None:
        inspect_tool_edit(**kwargs)

    def on_transform_tool_result(
        tool_name: str = "",
        args: Optional[dict[str, Any]] = None,
        result: Any = None,
        task_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        return inspect_tool_edit(tool_name, args, result, task_id, deliver=True, **kwargs)

    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("transform_tool_result", on_transform_tool_result)

    # 3. @ContextReferenceProviders (@sym:, @impact:, @hotspots:)
    ctx.register_context_reference(LocusSymbolReferenceProvider(ctx))
    ctx.register_context_reference(LocusImpactReferenceProvider(ctx))
    ctx.register_context_reference(LocusHotspotsReferenceProvider(ctx))

    # 4. Slash Command (/locus) & CLI Subcommand (hermes locus)
    def slash_locus(raw_args: str = "", **kwargs: Any) -> str:
        return _handle_slash_command(ctx, raw_args, **kwargs)

    ctx.register_command(
        "locus",
        slash_locus,
        description="Explore codebase graph, blast radius, symbols & signature drift alerts",
        args_hint="[map|impact <target>|symbol <name>|hotspots|drift|reindex]",
    )
    ctx.register_cli_command(
        "locus",
        help="Codebase graph, blast-radius & signature drift analyzer",
        setup_fn=_setup_cli_parser,
        handler_fn=_handle_cli_command,
        description="Analyze repository architecture, symbol callers, transitive blast radius, and signature drift.",
    )

    # 5. Bundled Read-Only Skill (locus-codegraph:locus-navigation)
    skill_path = Path(__file__).resolve().parent / "skills" / "locus-navigation" / "SKILL.md"
    if skill_path.is_file():
        ctx.register_skill(
            "locus-navigation",
            skill_path,
            description=(
                "Architectural navigation, pre-refactor blast-radius checks, and "
                "@sym:/@impact:/@hotspots: context workflows."
            ),
        )
