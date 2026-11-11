"""Token-budgeted Markdown formatters for LLM tools/@-references and ANSI formatters for CLI."""

from __future__ import annotations

import json
from typing import Any


def _error_detail(data: dict[str, Any], default: str) -> str:
    message = str(data.get("error", default))
    choices = data.get("candidates") or []
    return message + ("\n" + "\n".join(f"- `{choice}`" for choice in choices) if choices else "")


def _coverage_lines(data: dict[str, Any]) -> list[str]:
    status = data.get("scan_status") or {}
    lines = []
    if not status:
        lines.append("- Index coverage has not been recorded; run `/locus reindex` before relying on this report.")
    if status.get("truncated"):
        lines.append(f"- **Partial index:** the {status.get('max_files', '?')}-file scan limit was reached. "
                     "Unindexed callers and tests are absent from this report.")
    errors = status.get("parse_errors") or []
    if errors:
        paths = ", ".join(f"`{item.get('path') or item.get('rel_path') or '?'}`" for item in errors[:5])
        lines.append(f"- **Incomplete Python analysis:** {len(errors)} file(s) could not be parsed: {paths}. "
                     "Any retained graph for those files may be stale; repair and reindex.")
    stale = status.get("stale_files") or []
    if stale:
        details = "; ".join(f"`{item.get('path') or '?'}` ({item.get('reason') or 'not scanned'})"
                            for item in stale[:5])
        omitted = f"; {len(stale) - 5} more" if len(stale) > 5 else ""
        lines.append(f"- **Stale index:** retained prior analysis for {len(stale)} file(s) unavailable to the scan: "
                     f"{details}{omitted}. Existing caller warnings remain unresolved; "
                     "restore scan access and reindex before relying on this graph.")
    return lines


def format_repo_map_markdown(data: dict[str, Any]) -> str:
    """Render a compact, high-signal architectural map for LLM context or slash commands."""
    if not data.get("ok"):
        return "Locus Codegraph RepoMap Error: " + _error_detail(data, "unknown error")

    langs = ", ".join(f"{k}: {v}" for k, v in data.get("languages", {}).items()) or "none"
    lines: list[str] = [
        f"## Locus Codegraph Architectural Map (`{data.get('file_count', 0)}` files, "
        f"`{data.get('symbol_count', 0)}` symbols, `{data.get('edge_count', 0)}` dependency edges)",
        f"- Languages: {langs}",
    ]
    lines.extend(_coverage_lines(data))
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
        return "Locus Codegraph Symbol Lookup: " + _error_detail(data, "not found")

    sym = data["symbol"]
    lines: list[str] = [
        f"## Symbol `{sym['qualified_name']}` ({sym['kind']})",
        f"- Defined at: `{sym['rel_path']}:{sym['line_start']}-{sym['line_end']}`",
        f"- Signature: `{sym['signature']}`",
    ]
    lines.extend(_coverage_lines(data))
    if sym.get("docstring"):
        lines.append(f"- Doc: {sym['docstring']}")

    callers = data.get("callers") or []
    caller_count = data.get("caller_count", len(callers))
    lines.append(f"\n### Call Sites ({caller_count})")
    if callers:
        for c in callers[:15]:
            arg_note = f" ({c['arg_count']} args)" if c.get("arg_count", -1) >= 0 else ""
            lines.append(f"- `{c['path']}:{c['line']}` in `{c['caller_symbol']}`{arg_note}")
        if caller_count > min(15, len(callers)):
            lines.append(f"- Showing {min(15, len(callers))} of {caller_count} confirmed call sites.")
    else:
        lines.append("- _(no indexed workspace call sites)_")

    callees = data.get("callees") or []
    if callees:
        callee_count = data.get("callee_count", len(callees))
        lines.append(f"\n### Outgoing Calls ({callee_count})")
        for cal in callees[:12]:
            target_loc = f" -> `{cal['resolved_path']}`" if cal.get("resolved_path") else ""
            lines.append(f"- `L{cal['line']}` `{cal['callee_name']}()`{target_loc}")
        if callee_count > min(12, len(callees)):
            lines.append(f"- Showing {min(12, len(callees))} of {callee_count} outgoing calls.")

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
        return "Locus Codegraph Impact Analysis: " + _error_detail(data, "target not found")

    sym_part = (
        f" · symbol `{data['matched_symbol']['qualified_name']}`"
        if data.get("matched_symbol")
        else ""
    )
    lines: list[str] = [
        f"## Blast-Radius Report: `{data['resolved_file']}`{sym_part}",
        f"- **Risk Level**: **{data['risk_level']}** (heuristic score `{data['impact_score']}/100`, not a failure probability)",
        f"- **Direct Dependents**: `{len(data['direct_dependents'])}` · "
        f"**Transitive Dependents**: `{len(data['transitive_dependents'])}` · "
        f"**Suggested Tests**: `{len(data['covering_tests'])}` (inferred, not execution coverage)",
    ]
    lines.extend(_coverage_lines(data))

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
        call_count = data.get("external_call_count", len(calls))
        lines.append(f"\n### External Call Sites to Inspect ({call_count})")
        for c in calls[:15]:
            cname = c.get("callee_name") or (
                data["matched_symbol"]["name"] if data.get("matched_symbol") else ""
            )
            arg_str = f"{c['arg_count']} args" if c.get("arg_count", -1) >= 0 else "varargs"
            lines.append(f"- `{c['caller_path']}:{c['line_no']}` (`{c['caller_symbol']}` calls `{cname}` with {arg_str})")
        if call_count > min(15, len(calls)):
            lines.append(f"- Showing {min(15, len(calls))} of {call_count} confirmed external call sites.")

    tests = data.get("covering_tests") or []
    lines.append("\n### Recommended Verification Tests")
    if tests:
        for t in tests[:10]:
            lines.append(f"- `{t['rel_path']}`")
    else:
        lines.append("- No related test files were inferred from the indexed graph; inspect the project's test suite.")

    return "\n".join(lines)


def format_hotspots_markdown(data: dict[str, Any]) -> str:
    """Render circular dependencies, god modules, untested core files, and open drift."""
    if not data.get("ok"):
        return f"Locus Codegraph Hotspots Error: {data.get('error', 'unknown error')}"

    lines: list[str] = ["## Locus Codegraph Architectural Hotspots & Health"]
    lines.extend(_coverage_lines(data))

    drifts = data.get("open_drift_alerts") or []
    count = data.get("open_drift_count", len(drifts))
    lines.append(f"\n### Open Signature Drift Alerts ({count})")
    if drifts:
        for d in drifts[:10]:
            lines.append(f"- 🚨 **`{d['edited_path']}` · `{d['symbol_name']}`**: {d['reason']}")
            for caller in d.get("affected_callers", [])[:5]:
                detail = caller.get("incompatibility")
                suffix = f" — {detail}" if detail else ""
                lines.append(f"  - `{caller['path']}:{caller['line']}` (`{caller['caller']}`){suffix}")
        if count > min(10, len(drifts)):
            lines.append(f"- Showing {min(10, len(drifts))} of {count} alerts. Inspect affected files with `locus_impact`.")
    else:
        lines.append("- No unresolved signature drift alerts in the indexed graph. Runtime correctness still requires tests.")

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
        lines.append("\n### Central Modules Without Inferred Test Links")
        for u in untested[:8]:
            lines.append(f"- `{u['rel_path']}` (fan-in `{u['fan_in']}`, `{u['line_count']}` lines, no inferred test links)")

    return "\n".join(lines)


def format_rehearsal_markdown(data: dict[str, Any]) -> str:
    """Render a signature rehearsal: a prediction made before any file is edited."""
    if not data.get("ok"):
        lines = [f"### Locus Rehearsal\n{_error_detail(data, 'Rehearsal unavailable.')}"]
        if data.get("symbol"):
            lines.extend(_coverage_lines(data))
        return "\n".join(lines)
    sym = data["symbol"]
    breaks, review = data.get("breaks", []), data.get("needs_review", [])
    checked = data.get("checked_call_sites", 0)
    lines = [
        f"## Rehearsal: `{sym['qualified_name']}` in `{sym['rel_path']}` (nothing was edited)",
        f"- **Current:** `{data['current_signature']}`",
        f"- **Proposed parameters:** `{data['proposed_parameters']}`",
    ]
    if not data.get("changes_contract"):
        lines.append("- The proposal binds calls exactly as the current signature does.")
    if breaks:
        lines.append(f"- **{len(breaks)} of {checked} checked call site(s) would break**; "
                     f"{data.get('compatible_count', 0)} stay compatible"
                     + (f" and {len(review)} need review." if review else "."))
    else:
        lines.append(f"- **None of {checked} checked call site(s) would break.**")
    if breaks:
        lines.append("\n### Would break")
        for c in breaks[:30]:
            note = " (already incompatible today)" if c.get("already_incompatible") else ""
            lines.append(f"- `{c['path']}:{c['line']}` (`{c['caller']}`): {c['incompatibility']}{note}")
            if c.get("suggestion"):
                lines.append(f"  {c['suggestion']}")
        if len(breaks) > 30:
            lines.append(f"- {len(breaks) - 30} more breaking call site(s) omitted here.")
    if review:
        lines.append("\n### Needs review")
        for c in review[:15]:
            lines.append(f"- `{c['path']}:{c['line']}` (`{c['caller']}`): {c['reason']}")
        if len(review) > 15:
            lines.append(f"- {len(review) - 15} more call site(s) need review.")
    patch = data.get("caller_patch") or {}
    if patch.get("patch"):
        lines.append(f"\n### Caller patch for the rename ({patch['calls']} call(s) in {len(patch['files'])} file(s), not applied)")
        lines.append("Review it, then apply it with the patch tool alongside the definition change:")
        lines.append("```\n" + patch["patch"] + "\n```")
    for item in patch.get("skipped", [])[:5]:
        lines.append(f"- No patch for `{item['path']}`: {item['reason']}.")
    unchecked = data.get("unchecked") or {}
    notes = []
    if unchecked.get("same_file"):
        notes.append(f"{unchecked['same_file']} call(s) named `{sym['name']}` inside `{sym['rel_path']}`")
    if unchecked.get("unresolved_same_name"):
        notes.append(f"{unchecked['unresolved_same_name']} same-named call(s) elsewhere with no proven target")
    lines.append("\n### Not checked")
    if notes:
        lines.append("- " + " and ".join(notes) + " were not checked.")
    lines.append("- Only statically proven cross-file calls are bound. No predicted break is not proof of "
                 "safety; run the relevant tests after the change.")
    lines.extend(_coverage_lines(data))
    return "\n".join(lines)


MAX_INLINE_REPAIR_PATCH_CHARS = 6000


def format_drift_banner(alerts: list[dict[str, Any]], repair_patch: dict[str, Any] | None = None) -> str:
    """Render concise `[Locus Signature Drift Alert]` block appended to file-edit tool results.

    *repair_patch* is an optional caller patch for mechanical keyword renames.
    It is offered for review; Locus never applies it.
    """
    if not alerts:
        return ""
    lines: list[str] = [
        "[Locus Signature Drift Alert]",
        "Changed or removed signatures still affect these workspace callers:",
    ]
    for a in alerts[:5]:
        lines.append(
            f"- {a['severity'].upper()}: `{a['symbol_name']}` in `{a['edited_path']}` "
            f"(`{a['old_signature']}` -> `{a['new_signature']}`) — {a['reason']}"
        )
        for c in a.get("affected_callers", [])[:6]:
            detail = c.get("incompatibility")
            if not detail:
                detail = f"passes {c['arg_count']} arg(s)" if c.get("arg_count", -1) >= 0 else c.get("kind", "reference")
            action = "Inspect" if a["severity"] == "warning" or c.get("kind") == "import" else "Update caller"
            lines.append(f"  -> {action} `{c['path']}:{c['line']}` (`{c['caller']}`): {detail}")
            if c.get("suggestion"):
                lines.append(f"     {c['suggestion']}")
        # Keep detailed reasons for the first six, then compact locations. This
        # preserves useful reach without repeating a shared error dozens of times.
        extra_callers = a.get("affected_callers", [])[6:30]
        if extra_callers:
            locations = ", ".join(f"`{c['path']}:{c['line']}`" for c in extra_callers)
            lines.append(f"  -> Additional affected caller locations: {locations}")
        remaining = len(a.get("affected_callers", [])) - 30
        if remaining > 0:
            lines.append(f"  -> {remaining} additional affected caller(s) omitted. Request `locus_impact` for `{a['edited_path']}` to inspect the full alert.")
    if len(alerts) > 5:
        lines.append(f"{len(alerts) - 5} additional alert(s) omitted here; inspect `locus_hotspots`.")
    lines.append("Inspect and update the affected call sites above before concluding your task.")
    patch = (repair_patch or {}).get("patch") or ""
    if patch and len(patch) <= MAX_INLINE_REPAIR_PATCH_CHARS:
        lines.append(f"Ready caller patch for the renamed keyword ({repair_patch['calls']} call(s) in "
                     f"{len(repair_patch['files'])} file(s)). Review it, then apply it with the patch tool; "
                     "other listed call sites still need their own fix:")
        lines.append(patch)
    elif patch:
        lines.append("A caller patch for the renamed keyword is too large to include here; "
                     "apply the listed replacements file by file.")
    return "\n".join(lines)


def merge_drift_into_tool_result(raw_result: Any, banner: str) -> str:
    """Safely preserve original tool output (JSON or plain string) while attaching *banner*."""
    if not banner:
        return raw_result if isinstance(raw_result, str) else json.dumps(raw_result)

    if isinstance(raw_result, dict):
        # Preserve the same JSON envelope for native structured host results.
        raw_result = json.dumps(raw_result, ensure_ascii=False)

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
