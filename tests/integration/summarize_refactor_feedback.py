#!/usr/bin/env python3
"""Describe all attempts and complete matched pairs without imputing missing usage."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics


def metrics(rows):
    token_rows = [row for row in rows if isinstance(row.get("tokens"), dict)]
    token_keys = sorted({key for row in token_rows for key in row["tokens"]})
    tokens = {key: sum(row["tokens"].get(key, 0) for row in token_rows) for key in token_keys}
    costs = {}
    for row in rows:
        for key, value in row.get("reported_cost_fields", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                costs[key] = costs.get(key, 0) + value
    output = {"sessions": len(rows), "passed": sum(bool(row.get("pass")) for row in rows),
              "sessions_with_token_accounting": len(token_rows), "reported_tokens": tokens,
              "trace_reported_cost_fields": costs or None}
    for field in ("tool_calls", "search_calls", "read_calls", "edit_calls", "model_duration_ms"):
        values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
        output[field] = {"observations": len(values), "sum": sum(values),
                         "median": statistics.median(values) if values else None}
    return output


def summarize(summary, amendment=None):
    amendment = amendment or {}
    interrupted = amendment.get("interrupted_attempt")
    interrupted_key = ((interrupted["task"], interrupted["repeat"], interrupted["warning_enabled"])
                       if interrupted else None)
    reports = summary["reports"]
    keys = [(row["task"], row["repeat"], row["warning_enabled"]) for row in reports]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate attempted session")
    schedule = [tuple(job) for job in summary["schedule"]]
    if any(key not in schedule for key in keys):
        raise ValueError("Attempt outside the preregistered schedule")
    classified = {}
    rows = []
    for row, key in zip(reports, keys):
        if key == interrupted_key:
            classification = "evaluator_privacy_interruption"
        elif row.get("cli_exit") == 0 and row.get("model_exit") == 0:
            classification = "completed"
        else:
            classification = "incomplete_or_infrastructure"
        classified[key] = (row, classification)
        rows.append({"task": key[0], "repeat": key[1], "warning_enabled": key[2],
                     "classification": classification,
                     "task_pass": bool(row.get("pass")) if classification == "completed" else None,
                     "raw_attempt_pass": bool(row.get("pass")),
                     "test_summary": row.get("test_summary"), "tokens": row.get("tokens"),
                     "tool_calls": row.get("tool_calls"), "search_calls": row.get("search_calls"),
                     "read_calls": row.get("read_calls"), "edit_calls": row.get("edit_calls"),
                     "model_duration_ms": row.get("model_duration_ms")})
    completed = [row for row, classification in classified.values() if classification == "completed"]
    pair_checks = {(check["task"], check["repeat"]): check
                   for check in summary.get("pair_input_checks", [])}
    pairs, unpaired = [], []
    on_rows, off_rows = [], []
    for task, repeat in sorted({key[:2] for key in schedule}):
        on, off = classified.get((task, repeat, True)), classified.get((task, repeat, False))
        if not on or not off or on[1] != "completed" or off[1] != "completed":
            unpaired.append({"task": task, "repeat": repeat,
                             "on": on[1] if on else "not_started", "off": off[1] if off else "not_started"})
            continue
        if not pair_checks.get((task, repeat), {}).get("same_patch_result_except_warning"):
            raise ValueError(f"Complete pair lacks a valid matched-input check: {task}/{repeat}")
        on_rows.append(on[0]); off_rows.append(off[0])
        pairs.append({"task": task, "repeat": repeat, "on_pass": bool(on[0]["pass"]),
                      "off_pass": bool(off[0]["pass"]), "same_input_except_warning": True,
                      "on_tools": on[0].get("tool_calls"), "off_tools": off[0].get("tool_calls"),
                      "on_searches": on[0].get("search_calls"), "off_searches": off[0].get("search_calls"),
                      "on_tokens": (on[0].get("tokens") or {}).get("total"),
                      "off_tokens": (off[0].get("tokens") or {}).get("total")})
    return {
        "study_engine_sha256": summary["plugin_engine_sha256"], "model": summary["model"],
        "planned_attempts": len(schedule), "recorded_attempts": len(reports),
        "completed_sessions": len(completed), "not_started": [job for job in schedule if job not in classified],
        "interrupted_attempts": [row for row in rows if row["classification"] == "evaluator_privacy_interruption"],
        "other_incomplete_attempts": [row for row in rows if row["classification"] == "incomplete_or_infrastructure"],
        "all_recorded_usage": metrics(reports),
        "completed_sessions_by_arm": {
            "on": metrics([row for row in completed if row["warning_enabled"]]),
            "off": metrics([row for row in completed if not row["warning_enabled"]])},
        "complete_matched_pairs": len(pairs), "matched_pairs": pairs, "unpaired": unpaired,
        "matched_outcomes": {
            "both_pass": sum(pair["on_pass"] and pair["off_pass"] for pair in pairs),
            "on_only_pass": sum(pair["on_pass"] and not pair["off_pass"] for pair in pairs),
            "off_only_pass": sum(pair["off_pass"] and not pair["on_pass"] for pair in pairs),
            "neither_pass": sum(not pair["on_pass"] and not pair["off_pass"] for pair in pairs)},
        "matched_metrics": {"on": metrics(on_rows), "off": metrics(off_rows)},
        "sessions": rows,
        "limits": ["Descriptive results for four purposively selected tasks; not a representative benchmark.",
                   "Interrupted attempts remain in raw evidence, but do not contribute a comparative task failure.",
                   "Reported token totals include cached input; missing final usage is unknown, never zero.",
                   "No dollar estimate is inferred when provider cost fields are absent.",
                   "Both arms load and compute Locus drift; the comparison isolates warning delivery.",
                   "Protocol amendment narrows file access without changing candidate engine, tasks or scoring."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--amendment", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = json.loads((args.results / "summary.json").read_text())
    amendment = json.loads(args.amendment.read_text()) if args.amendment else None
    result = summarize(summary, amendment)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    print(f"Recorded {result['recorded_attempts']} attempts, {result['completed_sessions']} completed sessions, "
          f"{result['complete_matched_pairs']} complete matched pairs.")


if __name__ == "__main__":
    main()
