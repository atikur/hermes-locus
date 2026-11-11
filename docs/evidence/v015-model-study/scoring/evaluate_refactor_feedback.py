#!/usr/bin/env python3
"""Compare Hermes refactors with the Locus edit warning enabled and disabled.

Run with an installed Hermes interpreter. The source checkout must contain
the pinned upstream revision. Requires an OpenAI key and makes two model
sessions per task and repetition. The plugin is loaded in both arms.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from verify_hermes_refactor import RENAME, WORKSPACE_COMMIT, engine_digest, extract_commit, git_head
from verify_signature_corpus import callable_signature, binding_error, patch_text
import evaluation_boundary


MODEL = "gpt-5.4-mini"
TASKS = {
    "display": {
        "helper": "agent/display.py", "symbol": "prepare_tool_preview", "old": "max_len", "new": "limit",
        "sites": ["gateway/platforms/base.py:2014", "gateway/run_turn_progress.py:375",
                  "tests/agent/test_display.py:142", "tests/agent/test_display.py:155",
                  "tests/agent/test_display.py:164"],
        "test": "tests/agent/test_display.py",
    },
    "replay": {
        "helper": "agent/replay_cleanup.py", "symbol": "canonicalize_replay_history", "old": "now", "new": "clock_now",
        "sites": ["agent/turn_context.py:1454", "tests/agent/test_replay_cleanup.py:159",
                  "tests/agent/test_replay_cleanup.py:200", "tests/agent/test_replay_cleanup.py:215",
                  "tests/agent/test_replay_cleanup.py:217"],
        "test": "tests/agent/test_replay_cleanup.py",
    },
    "accounting": {
        "helper": "agent/aux_accounting.py", "symbol": "record_aux_usage", "old": "provider", "new": "provider_id",
        "sites": ["agent/auxiliary_client.py:6647", "plugins/image_gen/_common.py:270",
                  "tests/hermes_state/test_aux_usage_accounting.py:199"],
        "test": "tests/hermes_state/test_aux_usage_accounting.py",
    },
    "watchdog": {
        "helper": "agent/chat_completion_wait_notice.py", "symbol": "codex_watchdog_deadline",
        "old": "stale_timeout", "new": "silence_timeout",
        "sites": ["agent/chat_completion_nonstream.py:168", "tests/agent/test_codex_ttfb_watchdog.py:335",
                  "tests/agent/test_codex_ttfb_watchdog.py:458"],
        "test": "tests/agent/test_codex_ttfb_watchdog.py",
        "call_form": "module alias",
    },
}
SITE_PATTERN = re.compile(r"Update caller `([^`]+:\d+)`")
KEY_PATTERN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")
REPLAY_PROMPT = (
    "The keyword-only parameter `{old}` was renamed to `{new}` in "
    "`{symbol}` in `{helper}`, with its body updated to match. "
    "Here is the actual Hermes patch result.\n\n"
    "```json\n{patch_result}\n```\n\n"
    "Update every affected call while preserving behavior and the existing test "
    "assertions. Use file tools inside the current disposable checkout. A separate "
    "evaluator will run the tests. Summarize the changes."
)


def prompt_for(task, fixed_helper_edit=False):
    if fixed_helper_edit:
        return (
            "Rename the keyword-only parameter `max_len` to `limit` in "
            "`prepare_tool_preview`. Start by invoking `patch` with `mode: patch` "
            "and this exact payload. Wait for the result before looking for callers.\n\n"
            f"```text\n{RENAME}\n```\n\n"
            "Then update every affected call. Preserve all existing test assertions. "
            "Use file tools within the current disposable checkout. A separate evaluator "
            "will run the tests. Summarize the changes."
        )
    return (
        f"Rename the keyword-only parameter `{task['old']}` to `{task['new']}` in "
        f"`{task['symbol']}` in `{task['helper']}`. Update the parameter's use in the "
        "function body and preserve behavior. First read only that file and make a patch "
        "changing only the helper. Wait for that patch result before searching for, "
        "reading, or editing any callers. Then update every affected call. Preserve "
        "all existing test assertions. Use file tools within the current disposable "
        "checkout. A separate evaluator will run the tests. Summarize the changes."
    )


def shell_env():
    names = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SSL_CERT_FILE", "SSL_CERT_DIR")
    return {**{key: os.environ[key] for key in names if key in os.environ},
            "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"}


def load_key(profile):
    if key := os.environ.get("OPENAI_API_KEY"):
        return key
    if profile:
        for line in profile.read_text().splitlines():
            name, _, value = line.strip().removeprefix("export ").partition("=")
            if name.strip() == "OPENAI_API_KEY":
                fields = shlex.split(value, comments=True)
                if fields:
                    return fields[0]
    raise SystemExit("Supply OPENAI_API_KEY in the environment or pass --credentials-file.")


def fingerprint(root):
    files = {}
    for current, dirs, names in os.walk(root, followlinks=False):
        current = Path(current)
        for directory in dirs[:]:
            if (current / directory).is_symlink():
                dirs.remove(directory)
                names.append(directory)
        for name in names:
            path = current / name
            files[path.relative_to(root).as_posix()] = (
                ("link", os.readlink(path)) if path.is_symlink()
                else (bool(path.stat().st_mode & 0o111), hashlib.sha256(path.read_bytes()).hexdigest())
            )
    return files


def clone_tree(source, target):
    """Use APFS copy-on-write when available; each run still has isolated paths."""
    if sys.platform == "darwin":
        result = subprocess.run(["cp", "-cR", str(source), str(target)], capture_output=True)
        if result.returncode == 0:
            return
        if target.exists():
            shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)


def trees(root, path):
    return ast.parse((root / path).read_text(encoding="utf-8"), filename=path)


def calls(tree, symbol):
    return sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)
         and ((isinstance(node.func, ast.Name) and node.func.id == symbol)
              or (isinstance(node.func, ast.Attribute) and node.func.attr == symbol))),
        key=lambda node: (node.lineno, node.col_offset),
    )


def remove_docstring(function):
    if ast.get_docstring(function, clean=False) is not None:
        function.body.pop(0)


def helper_matches(task, baseline, workspace):
    helper = task["helper"]
    original = trees(baseline, helper)
    revised = trees(workspace, helper)
    function = next(node for node in revised.body
                    if isinstance(node, ast.FunctionDef) and node.name == task["symbol"])
    args = {arg.arg for arg in function.args.kwonlyargs}
    signature_ok = task["old"] not in args and task["new"] in args
    before = next(node for node in original.body
                  if isinstance(node, ast.FunctionDef) and node.name == task["symbol"])
    # Compare the entire helper file, allowing formatting and the helper's
    # docstring to change. Runtime statements can only rename the parameter.
    remove_docstring(before)
    remove_docstring(function)
    next(arg for arg in before.args.kwonlyargs if arg.arg == task["old"]).arg = task["new"]
    for node in ast.walk(before):
        if isinstance(node, ast.Name) and node.id == task["old"]:
            node.id = task["new"]
    return signature_ok, ast.dump(original) == ast.dump(revised)


def verify_source(task, baseline, workspace, baseline_files):
    current_files = fingerprint(workspace)
    changed = sorted(name for name in baseline_files.keys() | current_files.keys()
                     if baseline_files.get(name) != current_files.get(name))
    helper = task["helper"]
    expected_files = {helper, *(site.rsplit(":", 1)[0] for site in task["sites"])}
    # Compare each caller file against the original AST with only its call keyword
    # updated. This accepts formatting changes and rejects changes to assertions.
    verified = []
    binding_errors = {}
    try:
        for path in sorted(expected_files - {helper}):
            expected = trees(baseline, path)
            path_sites = {int(site.rsplit(":", 1)[1]) for site in task["sites"]
                          if site.rsplit(":", 1)[0] == path}
            found = set()
            for call in calls(expected, task["symbol"]):
                for keyword in call.keywords:
                    if call.lineno in path_sites and keyword.arg == task["old"]:
                        keyword.arg = task["new"]
                        found.add(call.lineno)
            if found != path_sites:
                raise ValueError(f"Source pin no longer matches expected caller lines in {path}")
            actual = trees(workspace, path)
            if ast.dump(expected) == ast.dump(actual):
                verified.extend(f"{path}:{line}" for line in sorted(found))
            # CPython, rather than Locus, decides whether the repaired calls bind.
            signature = callable_signature((workspace / helper).read_text(), task["symbol"])
            for call in calls(actual, task["symbol"]):
                if any(keyword.arg == task["new"] for keyword in call.keywords):
                    binding_errors[f"{path}:{call.lineno}"] = binding_error(signature, call)
        signature_ok, helper_rename_ok = helper_matches(task, baseline, workspace)
    except (FileNotFoundError, SyntaxError, StopIteration, ValueError, AssertionError) as exc:
        signature_ok = helper_rename_ok = False
    return {"changed_files": changed, "expected_files_only": set(changed) == expected_files,
            "unexpected_files": sorted(set(changed) - expected_files),
            "changed_file_fingerprints": {name: {"before": baseline_files.get(name), "after": current_files.get(name)}
                                          for name in changed},
            "helper_signature_ok": signature_ok, "helper_rename_ok": helper_rename_ok,
            "verified_caller_sites": sorted(verified), "expected_caller_sites": sorted(task["sites"]),
            "repaired_call_binding_errors": binding_errors,
            "source_file_count_compared": len(baseline_files.keys() | current_files.keys())}


def helper_patch(task, baseline, *, source_only=False):
    """Rename the pinned helper using AST source coordinates, before either model runs."""
    before = (baseline / task["helper"]).read_text(encoding="utf-8")
    tree = ast.parse(before)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == task["symbol"])
    parameter = next(arg for arg in function.args.kwonlyargs if arg.arg == task["old"])
    nodes = [parameter, *(node for node in ast.walk(function)
                          if isinstance(node, ast.Name) and node.id == task["old"])]
    lines = before.splitlines(keepends=True)
    # AST offsets are UTF-8 byte offsets, not character offsets.
    for node in sorted(nodes, key=lambda node: (node.lineno, node.col_offset), reverse=True):
        raw = lines[node.lineno - 1].encode("utf-8")
        start = node.col_offset
        assert raw[start:start + len(task["old"])] == task["old"].encode()
        lines[node.lineno - 1] = (raw[:start] + task["new"].encode()
                                + raw[start + len(task["old"]):]).decode("utf-8")
    after = "".join(lines)
    return after if source_only else patch_text(task["helper"], before, after)


def randomized_jobs(tasks, repeats, seed):
    """Seeded randomized blocks, balancing on/off first within each task."""
    rng = random.Random(seed)
    pairs = []
    for name in tasks:
        first = bool(rng.getrandbits(1))
        for repeat in range(1, repeats + 1):
            on_first = first if repeat % 2 else not first
            pairs.append([(name, repeat, on_first), (name, repeat, not on_first)])
    rng.shuffle(pairs)
    return [job for pair in pairs for job in pair]


def aggregate(reports):
    summary = {}
    for enabled in (True, False):
        arm = [report for report in reports if report["warning_enabled"] == enabled]
        tokens = {}
        for report in arm:
            for key, value in (report.get("tokens") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    tokens[key] = tokens.get(key, 0) + value
        cost_fields = {}
        for report in arm:
            for key, value in report.get("reported_cost_fields", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost_fields[key] = cost_fields.get(key, 0) + value
        summary["warning_on" if enabled else "warning_off"] = {
            "runs": len(arm), "passed": sum(report.get("pass", False) for report in arm),
            "infrastructure_failures": sum(bool(report.get("infrastructure_error") or report.get("provider_infrastructure_error")) for report in arm),
            "tokens": tokens, "runs_with_token_accounting": sum(bool(report.get("tokens")) for report in arm),
            "reported_cost_fields": cost_fields,
            "cost_note": "Only actual trace cost fields; no assumed token prices or missing-cost imputation.",
        }
    return summary


def stop_reason(reports, session_token_limit, total_token_limit, consecutive_error_limit):
    """Predeclared spending/infrastructure stops; never select by task success."""
    totals = [(row.get("tokens") or {}).get("total") for row in reports]
    known = [value for value in totals if isinstance(value, (int, float))]
    if known and known[-1] > session_token_limit:
        return "session_token_anomaly"
    if sum(known) > total_token_limit:
        return "total_token_limit"
    trailing = 0
    for row in reversed(reports):
        if not (row.get("infrastructure_error") or row.get("provider_infrastructure_error")):
            break
        trailing += 1
    if trailing >= consecutive_error_limit:
        return "repeated_infrastructure_errors"
    return None


def audit_pair_results(output, reports):
    """Verify the paired prompt inputs differ only by the Locus warning payload."""
    checks = []
    for name, repeat in sorted({(row["task"], row["repeat"]) for row in reports}):
        pair = []
        for enabled in (True, False):
            label = f"{name}-{repeat}-{'warning-on' if enabled else 'warning-off'}"
            path = output / label / "helper-edit-result.json"
            if path.exists():
                result = json.loads(path.read_text())
                # Alert is also appended to output/message in some host versions.
                warning = result.pop("locus_drift_alert", None)
                if warning:
                    for key, value in result.items():
                        if isinstance(value, str):
                            result[key] = value.replace("\n\n" + warning, "").replace(warning, "")
                pair.append(result)
        checks.append({"task": name, "repeat": repeat, "available_arms": len(pair),
                       "same_patch_result_except_warning": len(pair) == 2 and pair[0] == pair[1]})
    return checks


def run_tests(task, workspace, home, args):
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--noconftest",
           "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", task["test"]]
    env = {**shell_env(), "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
           "PYTHONPATH": os.pathsep.join((str(workspace), str(args.pytest_deps))),
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, timeout=90)
    return result.returncode, result.stdout, result.stderr


def preflight_outcome_oracle(name, task, baseline, baseline_files, inputs, args):
    """Demonstrate that the independent outcome audit rejects breakage and accepts repair."""
    workspace = inputs / ("oracle-" + name)
    clone_tree(baseline, workspace)
    (workspace / task["helper"]).write_text(helper_patch(task, baseline, source_only=True), encoding="utf-8")
    broken_exit, broken_out, broken_err = run_tests(task, workspace, inputs / (name + "-broken-home"), args)
    # Only the predeclared caller keyword positions may change.
    for filename in sorted({site.rsplit(":", 1)[0] for site in task["sites"]}):
        path = workspace / filename
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        wanted = {int(site.rsplit(":", 1)[1]) for site in task["sites"] if site.rsplit(":", 1)[0] == filename}
        replacements = []
        found = set()
        for call in calls(ast.parse("".join(lines)), task["symbol"]):
            if call.lineno not in wanted:
                continue
            for keyword in call.keywords:
                if keyword.arg == task["old"]:
                    replacements.append(keyword)
                    found.add(call.lineno)
        assert found == wanted, (name, filename, sorted(found), sorted(wanted))
        for keyword in sorted(replacements, key=lambda node: (node.lineno, node.col_offset), reverse=True):
            raw = lines[keyword.lineno - 1].encode()
            start = keyword.col_offset
            assert raw[start:start + len(task["old"])] == task["old"].encode()
            lines[keyword.lineno - 1] = (raw[:start] + task["new"].encode()
                                      + raw[start + len(task["old"]):]).decode()
        path.write_text("".join(lines), encoding="utf-8")
    repaired_exit, repaired_out, repaired_err = run_tests(task, workspace, inputs / (name + "-repaired-home"), args)
    audit = verify_source(task, baseline, workspace, baseline_files)
    accepted = (audit["expected_files_only"] and audit["helper_signature_ok"] and audit["helper_rename_ok"]
                and audit["verified_caller_sites"] == audit["expected_caller_sites"]
                and all(value is None for value in audit["repaired_call_binding_errors"].values()))
    result = {"helper_only_test_exit": broken_exit, "reference_repair_test_exit": repaired_exit,
              "reference_repair_audit_pass": accepted, "reference_repair_source_audit": audit}
    out = args.output / "preflight"
    out.mkdir(exist_ok=True)
    (out / (name + "-broken-tests.txt")).write_text(broken_out + broken_err)
    (out / (name + "-repaired-tests.txt")).write_text(repaired_out + repaired_err)
    shutil.rmtree(workspace)
    assert broken_exit != 0 and repaired_exit == 0 and accepted, (name, result)
    return result


def analyze_events(raw, task):
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    tool_calls = [event for event in events if event.get("type") == "tool_use"]
    tool_results = [event for event in events if event.get("type") == "tool_result"]
    patches = [event for event in tool_results if event.get("name") in {"patch", "write_file"}]
    first_patch = patches[0] if patches else {}
    # Tool results are JSON strings containing a separately serialized alert.
    result = first_patch.get("output", "{}")
    decoded = json.loads(result) if isinstance(result, str) and result.startswith("{") else {}
    sites = sorted(SITE_PATTERN.findall(decoded.get("locus_drift_alert") or ""))
    patch_files = decoded.get("files_modified") or [decoded.get("resolved_path")]
    helper_paths = {task["helper"], "<workspace>/" + task["helper"], "./" + task["helper"]}
    before = events[:events.index(first_patch)] if first_patch else []
    pre_edit_lookup = [
        event for event in before
        if event.get("type") == "tool_use" and event.get("name") in {"search_files", "read_file"}
        and event.get("input", {}).get("path") not in helper_paths
    ]
    finish = next((event for event in reversed(events) if event.get("type") == "result"), {})
    return {
        "model_exit": finish.get("exit_code"), "tokens": finish.get("tokens"),
        "reported_cost_fields": {key: value for key, value in finish.items()
                                 if "cost" in key.lower()},
        "model_duration_ms": finish.get("duration_ms"),
        "first_patch_ok": bool(decoded.get("success", decoded.get("ok"))),
        "first_patch_files": patch_files,
        "first_patch_helper_only": bool(patch_files) and all(path in helper_paths for path in patch_files),
        "first_patch_alert_sites": sites,
        "outside_helper_lookups_before_first_patch": len(pre_edit_lookup) if first_patch else None,
        "tool_calls": len(tool_calls),
        "search_calls": sum(call.get("name") == "search_files" for call in tool_calls),
        "read_calls": sum(call.get("name") == "read_file" for call in tool_calls),
        "edit_calls": len(patches),
    }


def replay_helper_patch(workspace, args, env, task, baseline):
    # A separate no-credential process runs the real patch tool before the
    # model starts. Only the actual result is placed in the repair prompt.
    code = """import json
import os
import sys
from pathlib import Path
from agent.runtime_cwd import set_session_cwd
from hermes_cli import lifecycle, plugins
from model_tools import handle_function_call

set_session_cwd(str(Path.cwd()))
plugins.discover_plugins()
session = "paired-replay"
boundary_result = handle_function_call("read_file", {"path": os.environ["HERMES_HOME"] + "/plugins/locus-codegraph/__init__.py"},
                                      task_id=session, session_id=session)
assert "Evaluation file boundary:" in str(boundary_result), "Private plugin read was not blocked"
lifecycle.invoke_hook("on_session_start", session_id=session)
result = handle_function_call(
    "patch", {"mode": "patch", "patch": Path(sys.argv[2]).read_text()},
    task_id=session, session_id=session, turn_id="helper-edit",
    api_request_id="helper-edit", tool_call_id="helper-edit",
)
Path(sys.argv[1]).write_text(result if isinstance(result, str) else json.dumps(result))
"""
    pythonpath = os.pathsep.join((str(args.source), str(Path(__file__).resolve().parent)))
    output = workspace.parent / "replayed-edit.json"
    patch = workspace.parent / "helper-edit.patch"
    patch.write_text(helper_patch(task, baseline), encoding="utf-8")
    result = subprocess.run([sys.executable, "-c", code, str(output), str(patch)], cwd=workspace,
                            env={**env, "PYTHONPATH": pythonpath},
                            capture_output=True, text=True, timeout=220)
    if result.returncode or not output.is_file():
        raise RuntimeError(f"Helper patch did not return a result (exit {result.returncode}): {result.stderr}")
    return json.loads(output.read_text())


def evaluate(job, args, baseline, baseline_files, plugin, key):
    name, repeat, enabled = job
    task = TASKS[name]
    label = f"{name}-{repeat}-{'warning-on' if enabled else 'warning-off'}"
    reports = args.output / label
    reports.mkdir()
    with tempfile.TemporaryDirectory(prefix="locus-pair-", dir=args.scratch, ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        workspace, home = root / "workspace", root / "hermes-home"
        clone_tree(baseline, workspace)
        (home / "plugins").mkdir(parents=True)
        clone_tree(plugin, home / "plugins/locus-codegraph")
        evaluation_boundary.install(home)
        config = home / "config.yaml"
        config.write_text(
            "plugins:\n  enabled: [evaluation-boundary, locus-codegraph]\n  hook_callback_timeout: 120\n"
            "  entries:\n    locus-codegraph:\n      settings:\n"
            "        auto_index_on_start: true\n        max_files: 20000\n"
            f"        drift_guard: {str(enabled).lower()}\n"
            f"terminal:\n  backend: local\n  cwd: {json.dumps(str(workspace))}\n"
            "agent:\n  run_budget_seconds: 420\n  max_turns: 20\n", encoding="utf-8"
        )
        query = root / "prompt.txt"
        env = {**shell_env(), "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
               "LOCUS_WORKSPACE": str(workspace), "TERMINAL_CWD": str(workspace),
               "LOCUS_EVAL_ROOT": str(workspace), "HERMES_WRITE_SAFE_ROOT": str(workspace),
               "TERMINAL_ENV": "local"}
        command = [sys.executable, "-m", "hermes_cli.main", "chat", "--query-file", str(query),
                   "--oneshot", "--format", "stream-json", "--in", str(workspace),
                   "--provider", "openai-api", "--model", MODEL, "--reasoning", "low",
                   "--max-turns", "20", "--toolsets", "file", "--ignore-rules"]
        def redact(value):
            replacements = ((key, "<redacted>"), (str(workspace), "<workspace>"),
                            (str(root), "<scratch>"), (str(Path.home()), "<user-home>"))
            for old, new in replacements:
                value = value.replace(old, new)
            return KEY_PATTERN.sub("<redacted>", value)

        replayed_result = None
        if args.replay_helper_edit:
            initial = replay_helper_patch(workspace, args, env, task, baseline)
            assert initial.get("success"), initial
            assert all(helper_matches(task, baseline, workspace))
            assert len(initial.get("files_modified", [])) == 1
            assert (workspace / initial["files_modified"][0]).resolve() == workspace / task["helper"]
            replayed_result = json.loads(redact(json.dumps(initial)))
            formatted = json.dumps(replayed_result, ensure_ascii=False, sort_keys=True)
            (reports / "helper-edit-result.json").write_text(formatted + "\n", encoding="utf-8")
            prompt = REPLAY_PROMPT.format(**task, patch_result=formatted)
            config.write_text(config.read_text().replace("auto_index_on_start: true", "auto_index_on_start: false"))
            print(f"{label}: applied identical helper edit before model session", flush=True)
        else:
            prompt = prompt_for(task, args.fixed_helper_edit)
        query.write_text(prompt + "\n", encoding="utf-8")
        try:
            chat = subprocess.run(command, cwd=args.source, env={**env, "OPENAI_API_KEY": key}, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=465)
            raw, stderr, exit_code = chat.stdout, chat.stderr, chat.returncode
        except subprocess.TimeoutExpired as exc:
            raw = (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace")
            exit_code = 124
        raw = redact(raw)
        (reports / "events.jsonl").write_text(raw, encoding="utf-8")
        (reports / "stderr.txt").write_text(redact(stderr), encoding="utf-8")
        (reports / "prompt.txt").write_text(query.read_text(), encoding="utf-8")
        try:
            observed = analyze_events(raw, task)
        except (ValueError, TypeError, KeyError) as exc:
            observed = {"trace_error": type(exc).__name__}
        if replayed_result is not None:
            observed.update({
                "helper_edit_replayed_before_model": True,
                "first_patch_ok": True,
                "first_patch_files": replayed_result["files_modified"],
                "first_patch_helper_only": True,
                "outside_helper_lookups_before_first_patch": 0,
                "first_patch_alert_sites": sorted(SITE_PATTERN.findall(replayed_result.get("locus_drift_alert") or "")),
            })
        verify = verify_source(task, baseline, workspace, baseline_files)
        (reports / "source-audit.json").write_text(json.dumps(verify, indent=2) + "\n", encoding="utf-8")
        # Preserve complete diffs, not only a verdict, for an independent review.
        import difflib
        diffs = []
        for path in verify["changed_files"]:
            old, new = baseline / path, workspace / path
            if not old.is_symlink() and not new.is_symlink() and (not old.exists() or old.is_file()) and (not new.exists() or new.is_file()):
                try:
                    diffs.extend(difflib.unified_diff(old.read_text().splitlines(True) if old.exists() else [],
                                                    new.read_text().splitlines(True) if new.exists() else [],
                                                    fromfile="before/" + path if old.exists() else "/dev/null",
                                                    tofile="after/" + path if new.exists() else "/dev/null"))
                except UnicodeError:
                    diffs.append(f"Binary file changed: {path}\n")
            else:
                diffs.append(f"Symlink changed: {path}; fingerprints are in source-audit.json\n")
        (reports / "source.diff").write_text(redact("".join(diffs)), encoding="utf-8")
        try:
            test_exit, stdout, test_stderr = run_tests(task, workspace, root / "test-home", args)
        except subprocess.TimeoutExpired as exc:
            test_exit = 124
            stdout = exc.stdout or ""
            test_stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(test_stderr, bytes):
                test_stderr = test_stderr.decode("utf-8", errors="replace")
            test_stderr += "\nIndependent test evaluator timed out after 90 seconds.\n"
        (reports / "tests.txt").write_text(redact(stdout + test_stderr), encoding="utf-8")
        passed = (exit_code == 0 and observed.get("model_exit") == 0 and test_exit == 0
                  and verify["expected_files_only"] and verify["helper_signature_ok"]
                  and verify["helper_rename_ok"]
                  and all(error is None for error in verify["repaired_call_binding_errors"].values())
                  and verify["verified_caller_sites"] == verify["expected_caller_sites"])
        report = {"task": name, "repeat": repeat, "warning_enabled": enabled, "pass": passed,
                  "cli_exit": exit_code, **observed, **verify,
                  "test_exit": test_exit, "test_summary": redact(stdout.strip().splitlines()[-1] if stdout.strip() else test_stderr),
                  "trace_sha256": hashlib.sha256(raw.encode()).hexdigest()}
        if exit_code == 124 or (exit_code != 0 and observed.get("model_exit") is None):
            report["provider_infrastructure_error"] = "session_timeout_or_incomplete_exit"
        elif exit_code != 0 and re.search(r"rate.?limit|insufficient.quota|authentication|connection.error|api.connection|429|502|503", stderr, re.I):
            report["provider_infrastructure_error"] = "provider_error_signature"
        (reports / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"{label}: {'PASS' if passed else 'FAIL'}, tests: {report['test_summary']}, "
              f"searches: {report.get('search_calls')}, first warning sites: {len(report.get('first_patch_alert_sites', []))}", flush=True)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "runtime", "pytest-deps", "output", "scratch"):
        parser.add_argument(f"--{name}", type=lambda p: Path(p).resolve(), required=True)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--task", choices=TASKS, action="append")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261111, help="Predeclared randomization seed.")
    parser.add_argument("--plugin-ref", default="HEAD", help="Pinned revision, or WORKTREE for a hashed local snapshot.")
    parser.add_argument("--plugin-snapshot", type=lambda p: Path(p).resolve(), help="Use an already frozen plugin tree, without reading the changing worktree.")
    parser.add_argument("--run-models", action="store_true", help="Explicit opt-in to API calls; default only prepares protocol and baseline checks.")
    parser.add_argument("--max-session-tokens", type=int, default=500000)
    parser.add_argument("--max-total-tokens", type=int, default=5000000)
    parser.add_argument("--max-consecutive-infra-errors", type=int, default=2)
    helper_mode = parser.add_mutually_exclusive_group()
    helper_mode.add_argument("--fixed-helper-edit", action="store_true",
                             help="Supply an exact initial patch in the display prompt.")
    helper_mode.add_argument("--replay-helper-edit", action="store_true", default=True,
                             help="Dispatch the exact task helper patch before each model session (default).")
    helper_mode.add_argument("--staged-edit", action="store_true", help="Legacy staged protocol; cannot isolate first-edit differences.")
    args = parser.parse_args()
    tasks = args.task or list(TASKS)
    if args.repeats < 1:
        parser.error("Repeats must be positive.")
    if min(args.max_session_tokens, args.max_total_tokens, args.max_consecutive_infra_errors) < 1:
        parser.error("Stopping limits must be positive.")
    if args.fixed_helper_edit and tasks != ["display"]:
        parser.error("The legacy fixed helper mode requires --task display.")
    args.replay_helper_edit = not (args.fixed_helper_edit or args.staged_edit)
    args.output.mkdir(parents=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    plugin_repo = Path(__file__).resolve().parents[2]
    plugin_revision = git_head(plugin_repo) if args.plugin_ref == "WORKTREE" else subprocess.check_output(
        ["git", "-C", str(plugin_repo), "rev-parse", args.plugin_ref], text=True).strip()
    with tempfile.TemporaryDirectory(prefix="locus-eval-inputs-", dir=args.scratch, ignore_cleanup_errors=True) as inputs:
        inputs = Path(inputs)
        baseline, plugin = inputs / "baseline", inputs / "plugin"
        extract_commit(args.source, WORKSPACE_COMMIT, baseline)
        if args.plugin_snapshot:
            clone_tree(args.plugin_snapshot, plugin)
        elif args.plugin_ref == "WORKTREE":
            files = subprocess.check_output(["git", "-C", str(plugin_repo), "ls-files", "-z", "--cached",
                                             "--others", "--exclude-standard"]).decode().split("\0")
            for filename in filter(None, files):
                source = plugin_repo / filename
                if source.is_file():
                    target = plugin / filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        else:
            extract_commit(plugin_repo, plugin_revision, plugin)
        baseline_files = fingerprint(baseline)
        plugin_files = fingerprint(plugin)
        snapshot_hash = hashlib.sha256(json.dumps(plugin_files, sort_keys=True).encode()).hexdigest()
        (args.output / "plugin-file-manifest.json").write_text(json.dumps(plugin_files, indent=2) + "\n")
        preflight = {}
        outcome_oracle = {}
        for name in tasks:
            code, stdout, stderr = run_tests(TASKS[name], baseline, inputs / "preflight", args)
            if code:
                raise SystemExit(f"Untouched {name} tests failed before any model calls:\n{stdout}\n{stderr}")
            preflight[name] = stdout.strip().splitlines()[-1]
            print(f"{name} baseline: {preflight[name]}", flush=True)
            outcome_oracle[name] = preflight_outcome_oracle(name, TASKS[name], baseline, baseline_files, inputs, args)
            print(f"{name}: independent oracle rejects helper-only edit and accepts reference repair", flush=True)
        jobs = randomized_jobs(tasks, args.repeats, args.seed)
        protocol = {
            "protocol_version": 2, "state": "preregistered before model calls", "model": MODEL,
            "workspace_commit": WORKSPACE_COMMIT, "plugin_base_commit": plugin_revision,
            "plugin_source": "frozen-snapshot" if args.plugin_snapshot else args.plugin_ref,
            "plugin_engine_sha256": engine_digest(plugin),
            "plugin_snapshot_sha256": snapshot_hash,
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "file_boundary_sha256": hashlib.sha256(Path(evaluation_boundary.__file__).read_bytes()).hexdigest(),
            "privacy_boundary": "Identical pre-tool guard permits only read/search/write/patch paths inside the disposable pinned public checkout, resolving symlinks. Private installed plugin/profile paths and other tools are blocked. HERMES_WRITE_SAFE_ROOT is also set.",
            "baseline_files_sha256": hashlib.sha256(json.dumps(baseline_files, sort_keys=True).encode()).hexdigest(),
            "tasks": {name: {**TASKS[name], "helper_patch": helper_patch(TASKS[name], baseline)} for name in tasks},
            "randomization_seed": args.seed, "schedule": jobs,
            "preflight_outcome_oracle": outcome_oracle,
            "stopping_rules": {"max_session_tokens": args.max_session_tokens, "max_total_tokens": args.max_total_tokens,
                               "max_consecutive_infra_errors": args.max_consecutive_infra_errors,
                               "max_model_turns": 20, "model_budget_seconds": 420, "subprocess_timeout_seconds": 465,
                               "check_after": "each session; an in-progress bounded session can exceed a token threshold",
                               "paired_input_mismatch": "stop after a completed pair if inputs differ beyond warning"},
            "primary_outcome": "Successful CLI and model exit, all expected caller keyword repairs, preserved whole-file ASTs including assertions, no unexpected file changes, and upstream tests passing.",
            "secondary_outcomes": ["Exact sites delivered before lookup", "tool/search/read calls", "trace tokens", "trace-reported costs"],
            "limits": ["Purposively selected tasks, not a representative agent-success benchmark.",
                       "Existing display/replay/accounting tasks were used in development; watchdog is a new evaluation task, not a blind held-out benchmark.",
                       "Only warning delivery differs; both arms load and compute Locus drift, so this is not overhead versus no plugin.",
                       "Both arms use file tools; independent evaluator runs tests after completion, so the model has no test-repair loop.",
                       "Small stochastic sample; report every run and both paired outcomes, not a general percentage improvement.",
                       "Source equivalence is AST-based in expected files; formatting/comments and helper docstrings may differ."],
        }
        (args.output / "protocol.json").write_text(json.dumps(protocol, indent=2) + "\n", encoding="utf-8")
        reports = []
        summary = {"model": MODEL, "reasoning": "low", "toolsets": ["file"], "max_turns": 20,
                   "workspace_commit": WORKSPACE_COMMIT, "hermes_host_commit": git_head(args.source),
                   "plugin_commit": plugin_revision if not args.plugin_snapshot and args.plugin_ref != "WORKTREE" else None,
                   "plugin_base_commit": plugin_revision,
                   "plugin_source": "frozen-snapshot" if args.plugin_snapshot else args.plugin_ref,
                   "plugin_engine_sha256": engine_digest(plugin),
                   "plugin_snapshot_sha256": snapshot_hash,
                   "indexed_file_cap": 20000, "hook_timeout_seconds": 120, "parallel_sessions": 1,
                   "preflight": preflight, "fixed_helper_edit": args.fixed_helper_edit,
                   "replay_helper_edit": args.replay_helper_edit,
                   "randomization_seed": args.seed, "schedule": jobs,
                   "model_calls_authorized": args.run_models,
                   "prompts": {name: (REPLAY_PROMPT.format(**TASKS[name], patch_result="{patch_result}") if args.replay_helper_edit else
                                       prompt_for(TASKS[name], args.fixed_helper_edit)) for name in tasks},
                   "reports": reports}
        def save_summary():
            summary["aggregate"] = aggregate(reports)
            summary["pair_input_checks"] = audit_pair_results(args.output, reports) if args.replay_helper_edit else []
            (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        save_summary()
        if not args.run_models:
            print(f"Prepared {len(jobs)} planned sessions; no model calls. Review {args.output / 'protocol.json'}.")
            return
        key = load_key(args.credentials_file)
        for job in jobs:
            assert hashlib.sha256(json.dumps(fingerprint(plugin), sort_keys=True).encode()).hexdigest() == snapshot_hash
            try:
                report = evaluate(job, args, baseline, baseline_files, plugin, key)
            except Exception as exc:
                # Keep failed attempts in the denominator and continue the declared schedule.
                report = {"task": job[0], "repeat": job[1], "warning_enabled": job[2], "pass": False,
                          "infrastructure_error": type(exc).__name__}
                print(f"{job}: infrastructure failure ({type(exc).__name__}); retained in report", flush=True)
            reports.append(report)
            save_summary()
            reason = stop_reason(reports, args.max_session_tokens, args.max_total_tokens, args.max_consecutive_infra_errors)
            if any(check["available_arms"] == 2 and not check["same_patch_result_except_warning"]
                   for check in summary["pair_input_checks"]):
                reason = "paired_input_mismatch"
            if reason:
                summary["stop_reason"] = reason
                summary["scheduled_not_started"] = jobs[len(reports):]
                save_summary()
                print(f"Stopped by preregistered rule: {reason}; {len(reports)}/{len(jobs)} sessions attempted.", flush=True)
                break
        else:
            summary["study_complete"] = True
            save_summary()


if __name__ == "__main__":
    main()
