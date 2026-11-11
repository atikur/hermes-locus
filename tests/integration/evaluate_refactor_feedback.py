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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from verify_hermes_refactor import WORKSPACE_COMMIT, engine_digest, extract_commit, git_head


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
        "sites": ["agent/auxiliary_client.py:6647", "tests/hermes_state/test_aux_usage_accounting.py:199"],
        "test": "tests/hermes_state/test_aux_usage_accounting.py",
    },
}
SITE_PATTERN = re.compile(r"Update caller `([^`]+:\d+)`")
KEY_PATTERN = re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def prompt_for(task):
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


def trees(root, path):
    return ast.parse((root / path).read_text(encoding="utf-8"), filename=path)


def calls(tree, symbol):
    return sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Name) and node.func.id == symbol),
        key=lambda node: (node.lineno, node.col_offset),
    )


def remove_docstring(function):
    if ast.get_docstring(function, clean=False) is not None:
        function.body.pop(0)


def verify_source(task, baseline, workspace, baseline_files):
    current_files = fingerprint(workspace)
    changed = sorted(name for name in baseline_files.keys() | current_files.keys()
                     if baseline_files.get(name) != current_files.get(name))
    helper = task["helper"]
    expected_files = {helper, *(site.rsplit(":", 1)[0] for site in task["sites"])}
    # Compare each caller file against the original AST with only its call keyword
    # updated. This accepts formatting changes and rejects changes to assertions.
    verified = []
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
            if ast.dump(expected) == ast.dump(trees(workspace, path)):
                verified.extend(f"{path}:{line}" for line in sorted(found))
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
        helper_rename_ok = ast.dump(original) == ast.dump(revised)
    except (FileNotFoundError, SyntaxError, StopIteration):
        signature_ok = helper_rename_ok = False
    return {"changed_files": changed, "expected_files_only": set(changed) == expected_files,
            "helper_signature_ok": signature_ok, "helper_rename_ok": helper_rename_ok,
            "verified_caller_sites": sorted(verified), "expected_caller_sites": sorted(task["sites"])}


def run_tests(task, workspace, home, args):
    cmd = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--noconftest",
           "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", task["test"]]
    env = {**shell_env(), "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
           "PYTHONPATH": os.pathsep.join((str(workspace), str(args.pytest_deps))),
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    result = subprocess.run(cmd, cwd=workspace, env=env, capture_output=True, text=True, timeout=90)
    return result.returncode, result.stdout, result.stderr


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


def evaluate(job, args, baseline, baseline_files, plugin, key):
    name, repeat, enabled = job
    task = TASKS[name]
    label = f"{name}-{repeat}-{'warning-on' if enabled else 'warning-off'}"
    reports = args.output / label
    reports.mkdir()
    with tempfile.TemporaryDirectory(prefix="locus-pair-", dir=args.scratch, ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        workspace, home = root / "workspace", root / "hermes-home"
        shutil.copytree(baseline, workspace, symlinks=True)
        shutil.copytree(plugin, home / "plugins/locus-codegraph", symlinks=True)
        (home / "config.yaml").write_text(
            "plugins:\n  enabled: [locus-codegraph]\n  hook_callback_timeout: 120\n"
            "  entries:\n    locus-codegraph:\n      settings:\n"
            "        auto_index_on_start: true\n        max_files: 20000\n"
            f"        drift_guard: {str(enabled).lower()}\n"
            f"terminal:\n  backend: local\n  cwd: {json.dumps(str(workspace))}\n"
            "agent:\n  run_budget_seconds: 420\n  max_turns: 20\n", encoding="utf-8"
        )
        query = root / "prompt.txt"
        query.write_text(prompt_for(task) + "\n", encoding="utf-8")
        env = {**shell_env(), "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
               "LOCUS_WORKSPACE": str(workspace), "TERMINAL_CWD": str(workspace),
               "TERMINAL_ENV": "local", "OPENAI_API_KEY": key}
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

        try:
            chat = subprocess.run(command, cwd=args.source, env=env, stdin=subprocess.DEVNULL,
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
        verify = verify_source(task, baseline, workspace, baseline_files)
        test_exit, stdout, test_stderr = run_tests(task, workspace, root / "test-home", args)
        passed = (exit_code == 0 and observed.get("model_exit") == 0 and test_exit == 0
                  and verify["expected_files_only"] and verify["helper_signature_ok"]
                  and verify["helper_rename_ok"]
                  and verify["verified_caller_sites"] == verify["expected_caller_sites"])
        report = {"task": name, "repeat": repeat, "warning_enabled": enabled, "pass": passed,
                  "cli_exit": exit_code, **observed, **verify,
                  "test_exit": test_exit, "test_summary": redact(stdout.strip().splitlines()[-1] if stdout.strip() else test_stderr),
                  "trace_sha256": hashlib.sha256(raw.encode()).hexdigest()}
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
    args = parser.parse_args()
    tasks = args.task or list(TASKS)
    if args.repeats < 1:
        parser.error("Repeats must be positive.")
    args.output.mkdir(parents=True)
    args.scratch.mkdir(parents=True, exist_ok=True)
    plugin_repo = Path(__file__).resolve().parents[2]
    plugin_revision = git_head(plugin_repo)
    key = load_key(args.credentials_file)
    with tempfile.TemporaryDirectory(prefix="locus-eval-inputs-", dir=args.scratch, ignore_cleanup_errors=True) as inputs:
        inputs = Path(inputs)
        baseline, plugin = inputs / "baseline", inputs / "plugin"
        extract_commit(args.source, WORKSPACE_COMMIT, baseline)
        extract_commit(plugin_repo, plugin_revision, plugin)
        baseline_files = fingerprint(baseline)
        preflight = {}
        for name in tasks:
            code, stdout, stderr = run_tests(TASKS[name], baseline, inputs / "preflight", args)
            if code:
                raise SystemExit(f"Untouched {name} tests failed before any model calls:\n{stdout}\n{stderr}")
            preflight[name] = stdout.strip().splitlines()[-1]
            print(f"{name} baseline: {preflight[name]}", flush=True)
        jobs = [(name, repeat, enabled) for repeat in range(1, args.repeats + 1)
                for name in tasks for enabled in ((True, False) if repeat % 2 else (False, True))]
        reports = [evaluate(job, args, baseline, baseline_files, plugin, key) for job in jobs]
        summary = {"model": MODEL, "reasoning": "low", "toolsets": ["file"], "max_turns": 20,
                   "workspace_commit": WORKSPACE_COMMIT, "hermes_host_commit": git_head(args.source),
                   "plugin_commit": plugin_revision, "plugin_engine_sha256": engine_digest(plugin),
                   "indexed_file_cap": 20000, "hook_timeout_seconds": 120, "parallel_sessions": 1,
                   "preflight": preflight, "prompts": {name: prompt_for(TASKS[name]) for name in tasks},
                   "reports": reports}
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
