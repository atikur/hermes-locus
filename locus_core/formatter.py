"""Token-budgeted Markdown formatters for LLM tools/@-references and ANSI formatters for CLI."""

from __future__ import annotations

import json
from typing import Any


def format_repo_map_markdown(data: dict[str, Any]) -> str:
    """Render a compact, high-signal architectural map for LLM context or slash commands."""
    if not data.get("ok"):
        return f"Locus RepoMap Error: {data.get('error', 'unknown error')}"

    langs = ", ".join(f"{k}: {v}" for k, v in data.get("languages", {}).items()) or "none"
    lines: list[str] = [
        f"## Locus Architectural Map (`{data.get('file_count', 0)}` files, "
        f"`{data.get('symbol_count', 0)}` symbols, `{data.get('edge_count', 0)}` dependency edges)",
        f"- Languages: {langs}",
    ]
    if data.get("open_drift_count", 0):
        lines.append(f"- ⚠️ Open signature drift alerts: **{data['open_drift_count']}**")

    cycles = data.get("cycles") or []
    if cycles:
        lines.append(f"- 🔄 Circular dependencies detected: **{len(cycles)}**")
        for cyc in cycles[:3]:
            lines.append(f"  - `{' -> '.join(cyc)}`")

    lines.append("\n### Top Central Modules (PageRank)")
    for idx, mod in enumerate(data.get("modules", []), start=1):
        pr_pct = round(float(mod.get("pagerank", 0.0)) * 100.0, 2)
        lines.append(
            f"{idx}. **`{mod['rel_path']}`** "
            f"(PR `{pr_pct}%` · fan-in `{mod['fan_in']}` · fan-out `{mod['fan_out']}` · `{mod['line_count']}` lines)"
        )
        for sym in mod.get("top_symbols", [])[:6]:
            lines.append(f"   - `L{sym['line']}` `{sym['signature']}`")

    return "\n".join(lines)


def format_symbol_markdown(data: dict[str, Any]) -> str:
    """Render symbol definition, callers, callees, and bounded source snippet."""
    if not data.get("ok"):
        return f"Locus Symbol Lookup: {data.get('error', 'not found')}"

    sym = data["symbol"]
    lines: list[str] = [
        f"## Symbol `{sym['qualified_name']}` ({sym['kind']})",
        f"- Defined at: `{sym['rel_path']}:{sym['line_start']}-{sym['line_end']}`",
        f"- Signature: `{sym['signature']}`",
    ]
    if sym.get("docstring"):
        lines.append(f"- Doc: {sym['docstring']}")

    callers = data.get("callers") or []
    lines.append(f"\n### Call Sites ({len(callers)})")
    if callers:
        for c in callers[:15]:
            arg_note = f" ({c['arg_count']} args)" if c.get("arg_count", -1) >= 0 else ""
            lines.append(f"- `{c['path']}:{c['line']}` in `{c['caller_symbol']}`{arg_note}")
    else:
        lines.append("- _(no indexed workspace call sites)_")

    callees = data.get("callees") or []
    if callees:
        lines.append(f"\n### Outgoing Calls ({len(callees)})")
        for cal in callees[:12]:
            target_loc = f" -> `{cal['resolved_path']}`" if cal.get("resolved_path") else ""
            lines.append(f"- `L{cal['line']}` `{cal['callee_name']}()`{target_loc}")

    snippet = data.get("snippet") or ""
    if snippet:
        lines.append(f"\n### Source Excerpt (`{sym['rel_path']}`)")
        lines.append("```")
        lines.append(snippet)
        lines.append("```")

    other = data.get("other_matches") or []
    if other:
        alts = ", ".join(f"`{m['qualified_name']}` (`{m['rel_path']}:{m['line_start']}`)" for m in other[:4])
        lines.append(f"\n_Other matches_: {alts}")

    return "\n".join(lines)


def format_impact_markdown(data: dict[str, Any]) -> str:
    """Render transitive blast-radius analysis, call sites, covering tests, and drift warnings."""
    if not data.get("ok"):
        return f"Locus Impact Analysis: {data.get('error', 'target not found')}"

    sym_part = (
        f" · symbol `{data['matched_symbol']['qualified_name']}`"
        if data.get("matched_symbol")
        else ""
    )
    lines: list[str] = [
        f"## Blast-Radius Report: `{data['resolved_file']}`{sym_part}",
        f"- **Risk Level**: **{data['risk_level']}** (score `{data['impact_score']}/100`)",
        f"- **Direct Dependents**: `{len(data['direct_dependents'])}` · "
        f"**Transitive Dependents**: `{len(data['transitive_dependents'])}` · "
        f"**Covering Tests**: `{len(data['covering_tests'])}`",
    ]

    drifts = data.get("open_drift_alerts") or []
    if drifts:
        lines.append("\n### 🚨 Open Signature Drift Alerts")
        for d in drifts:
            lines.append(
                f"- **{d['symbol_name']}** (`{d['severity'].upper()}`): {d['reason']} "
                f"(`{d['old_signature']}` -> `{d['new_signature']}`)"
            )

    direct = data.get("direct_dependents") or []
    if direct:
        lines.append("\n### Direct Dependents (Hop 1)")
        for dep in direct[:15]:
            lines.append(f"- `{dep['rel_path']}` (fan-in `{dep['fan_in']}`, lines `{dep['line_count']}`)")
    else:
        lines.append("\n### Direct Dependents (Hop 1)\n- _(none)_")

    trans = data.get("transitive_dependents") or []
    if trans:
        lines.append("\n### Transitive Blast Radius (Hops 2-3)")
        for dep in trans[:15]:
            lines.append(f"- [hop {dep['depth']}] `{dep['rel_path']}` (via `{dep['via']}`)")

    calls = data.get("external_call_sites") or []
    if calls:
        lines.append("\n### External Call Sites to Inspect")
        for c in calls[:15]:
            cname = c.get("callee_name") or (
                data["matched_symbol"]["name"] if data.get("matched_symbol") else ""
            )
            arg_str = f"{c['arg_count']} args" if c.get("arg_count", -1) >= 0 else "varargs"
            lines.append(f"- `{c['caller_path']}:{c['line_no']}` (`{c['caller_symbol']}` calls `{cname}` with {arg_str})")

    tests = data.get("covering_tests") or []
    lines.append("\n### Recommended Verification Tests")
    if tests:
        for t in tests[:10]:
            lines.append(f"- `{t['rel_path']}`")
    else:
        lines.append("- ⚠️ **No covering test files detected** in reverse dependency graph — add targeted tests!")

    return "\n".join(lines)


def format_hotspots_markdown(data: dict[str, Any]) -> str:
    """Render circular dependencies, god modules, untested core files, and open drift."""
    if not data.get("ok"):
        return f"Locus Hotspots Error: {data.get('error', 'unknown error')}"

    lines: list[str] = ["## Locus Architectural Hotspots & Health"]

    drifts = data.get("open_drift_alerts") or []
    lines.append(f"\n### Open Signature Drift Alerts ({len(drifts)})")
    if drifts:
        for d in drifts[:10]:
            lines.append(f"- 🚨 **`{d['edited_path']}` · `{d['symbol_name']}`**: {d['reason']}")
            for caller in d.get("affected_callers", [])[:5]:
                lines.append(f"  - Broken caller: `{caller['path']}:{caller['line']}` (`{caller['caller']}`)")
    else:
        lines.append("- ✅ Zero unresolved signature drift alerts.")

    cycles = data.get("cycles") or []
    lines.append(f"\n### Circular Dependencies ({len(cycles)})")
    if cycles:
        for cyc in cycles[:8]:
            lines.append(f"- 🔄 `{' -> '.join(cyc)}`")
    else:
        lines.append("- ✅ Acyclic module graph (0 strongly connected import cycles).")

    gods = data.get("god_modules") or []
    if gods:
        lines.append("\n### High-Coupling Hub Modules")
        for g in gods[:8]:
            lines.append(
                f"- `{g['rel_path']}` — fan-in `{g['fan_in']}`, fan-out `{g['fan_out']}`, `{g['line_count']}` lines"
            )

    untested = data.get("untested_core") or []
    if untested:
        lines.append("\n### Untested High-Centrality Modules")
        for u in untested[:8]:
            lines.append(f"- ⚠️ `{u['rel_path']}` (fan-in `{u['fan_in']}`, `{u['line_count']}` lines, 0 covering tests)")

    return "\n".join(lines)


def format_drift_banner(alerts: list[dict[str, Any]]) -> str:
    """Render concise `[Locus Signature Drift Alert]` block appended to file-edit tool results."""
    if not alerts:
        return ""
    lines: list[str] = [
        "[Locus Signature Drift Alert]",
        "Your edit changed or removed exported symbol contract(s) with existing external callers:",
    ]
    for a in alerts[:5]:
        lines.append(
            f"- {a['severity'].upper()}: `{a['symbol_name']}` in `{a['edited_path']}` "
            f"(`{a['old_signature']}` -> `{a['new_signature']}`) — {a['reason']}"
        )
        for c in a.get("affected_callers", [])[:6]:
            arg_info = f"passes {c['arg_count']} arg(s)" if c.get("arg_count", -1) >= 0 else c.get("kind", "ref")
            lines.append(f"  -> Update caller `{c['path']}:{c['line']}` (`{c['caller']}`, {arg_info})")
    lines.append("Inspect and update the affected call sites above before concluding your task.")
    return "\n".join(lines)


def merge_drift_into_tool_result(raw_result: Any, banner: str) -> str:
    """Safely preserve original tool output (JSON or plain string) while attaching *banner*."""
    if not banner:
        return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

    if isinstance(raw_result, str):
        stripped = raw_result.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(raw_result)
                if isinstance(parsed, dict):
                    parsed["locus_drift_alert"] = banner
                    for text_key in ("output", "message", "result", "detail"):
                        if isinstance(parsed.get(text_key), str):
                            parsed[text_key] = f"{parsed[text_key]}\n\n{banner}"
                            break
                    return json.dumps(parsed, ensure_ascii=False)
            except ValueError:
                pass
        return f"{raw_result}\n\n{banner}"

    return f"{json.dumps(raw_result, ensure_ascii=False)}\n\n{banner}"
