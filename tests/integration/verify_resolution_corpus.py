#!/usr/bin/env python3
"""Supplement the preserved 19-case corpus with resolution and false-positive checks.

No model calls. Real source projections keep pinned original callers. Separate
constructed controls execute in isolated Python processes to label binding and
non-target/shadowing behavior. This is a targeted regression set, not a blind or
representative held-out benchmark. Every case is declared before running Locus.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from verify_hermes_refactor import WORKSPACE_COMMIT, engine_digest, extract_commit, git_head
from verify_signature_corpus import binding_error, callable_signature, definition, git, patch_text


PINS = {
    "agent/retry_utils.py": "fc1d702f263b243c4d1c708507a565411f9f6790",
    "agent/auxiliary_rate_limit_backoff.py": "27a5f0527c396b2b4d1d322e2b7fc6439fea3941",
    "tests/agent/test_retry_utils.py": "e090364669ea7a0e9d670e88dc8c126a5e32b00f",
    "agent/chat_completion_wait_notice.py": "5fc15959ccf740ac9b23816f89bbb5bb0012775f",
    "agent/chat_completion_nonstream.py": "62d5bd7af517815dc47009e2fe73488324bdb847",
    "tests/agent/test_codex_ttfb_watchdog.py": "7947a13f2f99a958167a22d3a044a6c019c10cb0",
    "agent/aux_accounting.py": "df0e1b1c6c7e0c7406d2e72960609fac0c66b6ca",
    "agent/auxiliary_client.py": "c7866a87a03e0283c41ec284ca66f21c96830013",
    "plugins/image_gen/_common.py": "8af6741ae47138021fec100c3d8f3628138348e1",
    "tests/hermes_state/test_aux_usage_accounting.py": "1d6a101b414f2d4ba4c90a9627eb2543581f30b5",
}
TARGETS = [
    {"name": "retry", "helper": "agent/retry_utils.py", "symbol": "jittered_backoff", "parameter": "max_delay",
     "files": ["agent/retry_utils.py", "agent/auxiliary_rate_limit_backoff.py", "tests/agent/test_retry_utils.py"],
     "sites": ["agent/retry_utils.py:167", "agent/auxiliary_rate_limit_backoff.py:45",
               "tests/agent/test_retry_utils.py:15", "tests/agent/test_retry_utils.py:24", "tests/agent/test_retry_utils.py:45"]},
    {"name": "watchdog", "helper": "agent/chat_completion_wait_notice.py", "symbol": "codex_watchdog_deadline",
     "parameter": "stale_timeout", "files": ["agent/chat_completion_wait_notice.py", "agent/chat_completion_nonstream.py",
                                               "tests/agent/test_codex_ttfb_watchdog.py"],
     "sites": ["agent/chat_completion_nonstream.py:168", "tests/agent/test_codex_ttfb_watchdog.py:335",
               "tests/agent/test_codex_ttfb_watchdog.py:458"]},
    {"name": "accounting", "helper": "agent/aux_accounting.py", "symbol": "record_aux_usage", "parameter": "provider",
     "files": ["agent/aux_accounting.py", "agent/auxiliary_client.py", "plugins/image_gen/_common.py",
               "tests/hermes_state/test_aux_usage_accounting.py"],
     "sites": ["agent/auxiliary_client.py:6647", "plugins/image_gen/_common.py:270",
               "tests/hermes_state/test_aux_usage_accounting.py:199", "tests/hermes_state/test_aux_usage_accounting.py:222",
               "tests/hermes_state/test_aux_usage_accounting.py:223"]},
]


def mutated(source, symbol, parameter, kind):
    node = definition(source, symbol)
    revised = copy.deepcopy(node)
    if kind == "keyword-rename":
        for child in ast.walk(revised):
            if isinstance(child, ast.arg) and child.arg == parameter:
                child.arg = "renamed_" + parameter
            elif isinstance(child, ast.Name) and child.id == parameter:
                child.id = "renamed_" + parameter
    elif kind in {"required-keyword", "optional-keyword"}:
        revised.args.kwonlyargs.append(ast.arg(arg="corpus_probe"))
        revised.args.kw_defaults.append(None if kind == "required-keyword" else ast.Constant(None))
    elif kind == "body-only":
        revised.body.append(ast.Pass())
    else:
        raise ValueError(kind)
    lines = source.splitlines(keepends=True)
    rendered = ast.unparse(revised) + "\n"
    old_line_count = node.end_lineno - node.lineno + 1
    assert len(rendered.splitlines()) <= old_line_count, "Preserve downstream caller locations in this corpus"
    rendered += "\n" * (old_line_count - len(rendered.splitlines()))
    return "".join(lines[:node.lineno - 1]) + rendered + "".join(lines[node.end_lineno:])


def real_cases(host):
    sources = {}
    for path, blob in PINS.items():
        raw = git(host, "show", f"{WORKSPACE_COMMIT}:{path}")
        assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == blob, path
        sources[path] = raw.decode()
    cases = []
    for target in TARGETS:
        files = {path: sources[path] for path in target["files"]}
        calls = {}
        for path, text in files.items():
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == target["symbol"]
                    or isinstance(node.func, ast.Attribute) and node.func.attr == target["symbol"]
                ):
                    site = f"{path}:{node.lineno}"
                    assert site not in calls
                    calls[site] = node
        assert set(calls) == set(target["sites"]), (target["name"], sorted(calls))
        before = files[target["helper"]]
        original_signature = callable_signature(before, target["symbol"])
        assert all(binding_error(original_signature, call) is None for call in calls.values())
        for kind in ("keyword-rename", "required-keyword", "optional-keyword", "body-only"):
            after = mutated(before, target["symbol"], target["parameter"], kind)
            signature = callable_signature(after, target["symbol"])
            cases.append({"case": f"hermes-{target['name']}-{kind}", "group": "pinned-real-source",
                          "files": files, "helper": target["helper"], "after": after,
                          "oracle": "CPython inspect.Signature.bind; original source call shapes, no Locus resolver",
                          "labels": {site: {"error": binding_error(signature, call),
                                            "scope": "same-file" if site.rsplit(":", 1)[0] == target["helper"] else "cross-file"}
                                     for site, call in calls.items()}})
    return cases


CONTROL_CALLERS = {
    "direct-alias": "from pkg.api import process as invoke\ndef run():\n    return invoke(1, limit=3)\n",
    "module-import": "import pkg.api\ndef run():\n    return pkg.api.process(1, limit=3)\n",
    "module-alias": "import pkg.api as api\ndef run():\n    return api.process(1, limit=3)\n",
    "from-module-alias": "from pkg import api as tools\ndef run():\n    return tools.process(1, limit=3)\n",
    "relative-module": "from . import api\ndef run():\n    return api.process(1, limit=3)\n",
    "repeated-local-import": "def first():\n    from pkg.api import process\n    return process(1, limit=3)\ndef second():\n    from pkg.api import process\n    return process(2, limit=3)\ndef run():\n    return first(), second()\n",
    "scoped-different-imports": "def first():\n    from pkg.api import process\n    return process(1, limit=3)\ndef second():\n    from pkg.other import process\n    return process(2, limit=3)\ndef run():\n    return first(), second()\n",
    "parameter-shadow": "from pkg import api\ndef invoke(api):\n    return api.process(1, limit=3)\ndef run():\n    from pkg import other\n    return invoke(other)\n",
    "assignment-shadow": "from pkg import api\nfrom pkg import other\napi = other\ndef run():\n    return api.process(1, limit=3)\n",
    "conditional-other-import": "if False:\n    from pkg.api import process\nelse:\n    from pkg.other import process\ndef run():\n    return process(1, limit=3)\n",
    "same-name-other-module": "from pkg.other import process\ndef run():\n    return process(1, limit=3)\n",
    "local-shadow": "from pkg.api import process\ndef run():\n    process = lambda value, *, limit=3: value + limit\n    return process(1, limit=3)\n",
    "unpacked-compatible": "from pkg.api import process\ndef run():\n    return process(*[1], **{'limit': 3})\n",
}


def executable_control(files, entry="run"):
    """Run only our tiny authored fixtures, never arbitrary upstream package code."""
    with tempfile.TemporaryDirectory(prefix="locus-binding-oracle-") as scratch:
        root = Path(scratch)
        for path, source in files.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source)
        assert entry in {"run", "first", "second"}
        code = "import json\ntry:\n from pkg import client\n value=getattr(client, " + repr(entry) + ")()\n print(json.dumps({'ok':True,'value':value}))\nexcept TypeError as exc:\n print(json.dumps({'ok':False,'error':str(exc)}))\n"
        result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                env={"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


def control_cases():
    cases = []
    for name, caller in CONTROL_CALLERS.items():
        before = "def process(value, *, limit=3):\n    return value + limit\n"
        # Unpacked call is a compatible addition control, not a resolvability claim.
        after = ("def process(value, *, limit=3, extra=None):\n    return value + limit\n" if name == "unpacked-compatible"
                 else "def process(value, *, ceiling=3):\n    return value + ceiling\n")
        files = {"pkg/__init__.py": "", "pkg/api.py": before, "pkg/other.py": before, "pkg/client.py": caller}
        entries = ["first", "second"] if name in {"repeated-local-import", "scoped-different-imports"} else ["run"]
        original = {entry: executable_control(files, entry) for entry in entries}
        changed = {entry: executable_control({**files, "pkg/api.py": after}, entry) for entry in entries}
        assert all(result["ok"] for result in original.values()), name
        sites = [f"pkg/client.py:{node.lineno}" for node in ast.walk(ast.parse(caller))
                 if isinstance(node, ast.Call) and (
                     isinstance(node.func, ast.Name) and node.func.id in {"process", "invoke"}
                     and any(keyword.arg == "limit" for keyword in node.keywords)
                     or isinstance(node.func, ast.Attribute) and node.func.attr == "process"
                     or name == "unpacked-compatible" and isinstance(node.func, ast.Name) and node.func.id == "process")]
        assert sites, name
        sites.sort(key=lambda site: int(site.rsplit(":", 1)[1]))
        labels = {site: {"error": changed[entries[index] if len(entries) > 1 else "run"].get("error"),
                         "scope": "cross-file"} for index, site in enumerate(sites)}
        cases.append({"case": "control-" + name, "fixture_name": name, "group": "constructed-control", "files": files,
                      "helper": "pkg/api.py", "after": after,
                      "oracle": "Executed authored fixture before/after in clean Python subprocesses",
                      "oracle_execution": {"before": original, "after": changed},
                      "labels": labels})
    # Arity changes must also avoid warnings for unrelated shadowed functions.
    # These are additional mutations of the same fixtures, not new unique sites.
    for original_case in list(cases):
        name = original_case["fixture_name"]
        if name == "unpacked-compatible":
            continue
        after = "def process(value, *, limit=3, required_probe):\n    return value + limit\n"
        entries = ["first", "second"] if name in {"repeated-local-import", "scoped-different-imports"} else ["run"]
        changed = {entry: executable_control({**original_case["files"], "pkg/api.py": after}, entry) for entry in entries}
        sites = sorted(original_case["labels"], key=lambda site: int(site.rsplit(":", 1)[1]))
        labels = {site: {"error": changed[entries[index] if len(entries) > 1 else "run"].get("error"),
                         "scope": "cross-file"} for index, site in enumerate(sites)}
        cases.append({**original_case, "case": original_case["case"] + "-required-keyword", "after": after,
                      "oracle_execution": {"before": original_case["oracle_execution"]["before"], "after": changed},
                      "labels": labels})
    return cases


def score(rows):
    groups = {}
    for row in rows:
        key = row["group"]
        group = groups.setdefault(key, {"cases": 0, "broken": 0, "named_broken": 0, "compatible": 0,
                                        "named_compatible": 0, "unlabelled_named": 0, "missed_sites": [],
                                        "false_positive_sites": [], "same_file_broken": 0})
        group["cases"] += 1
        named = set(row["named_sites"])
        for site, label in row["labels"].items():
            if label["error"] is not None:
                group["broken"] += 1
                group["named_broken"] += site in named
                group["same_file_broken"] += label["scope"] == "same-file"
                if site not in named:
                    group["missed_sites"].append({"case": row["case"], "site": site, "scope": label["scope"]})
            else:
                group["compatible"] += 1
                group["named_compatible"] += site in named
                if site in named:
                    group["false_positive_sites"].append({"case": row["case"], "site": site})
        group["unlabelled_named"] += len(named - row["labels"].keys())
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--plugin-ref", default="HEAD", help="Git revision or WORKTREE; raw source manifest is always recorded.")
    parser.add_argument("--plugin-snapshot", type=Path, help="Already frozen candidate plugin tree.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true", help="Create and independently label the corpus without loading Locus.")
    parser.add_argument("--check-cross-file", action="store_true", help="Fail on missed cross-file calls or false positives; retain same-file misses explicitly.")
    args = parser.parse_args()
    args.source, args.runtime, args.output = (path.resolve() for path in (args.source, args.runtime, args.output))
    args.plugin_snapshot = args.plugin_snapshot.resolve() if args.plugin_snapshot else None
    assert not args.output.exists(), "Refuse to overwrite any prior evidence. Use a new output path."
    cases = real_cases(args.source) + control_cases()
    report = {"protocol_version": 1, "model_calls": 0, "source_commit": WORKSPACE_COMMIT,
              "source_blob_pins": PINS, "selection": "Targeted extension declared before execution; not a blind held-out accuracy estimate.",
              "limits": ["Counts repeat source calls across mutations; report distinct locations separately.",
                         "Same-file calls remain in the denominator and are marked separately.",
                         "Constructed controls are separate from untouched real-source projections.",
                         "Original 19-case corpus and historical evidence remain separate and unchanged."],
              "planned_cases": len(cases), "cases": []}
    report["case_manifest"] = [{"case": case["case"], "group": case["group"], "labels": case["labels"],
                                "patch_sha256": hashlib.sha256(patch_text(case["helper"], case["files"][case["helper"]], case["after"]).encode()).hexdigest()}
                               for case in cases]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.prepare_only:
        report["status"] = "prepared-only"
        report["cases"] = cases
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Prepared {len(cases)} independently labelled cases; no Locus or model execution.")
        return
    plugin_repo = Path(__file__).resolve().parents[2]
    report["status"] = "running"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="locus-resolution-corpus-") as scratch:
        root = Path(scratch)
        home = root / "home"
        install = home / "plugins/locus-codegraph"
        if args.plugin_snapshot:
            shutil.copytree(args.plugin_snapshot, install)
        elif args.plugin_ref == "WORKTREE":
            filenames = git(plugin_repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
            for filename in filter(None, filenames):
                source = plugin_repo / filename
                if source.is_file():
                    dest = install / filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
        else:
            extract_commit(plugin_repo, args.plugin_ref, install)
        report.update({"plugin_ref": "frozen-snapshot" if args.plugin_snapshot else args.plugin_ref,
                       "plugin_base_commit": git_head(plugin_repo),
                       "plugin_engine_sha256": engine_digest(install)})
        (home / "config.yaml").write_text("plugins:\n  enabled: [locus-codegraph]\n  hook_callback_timeout: 120\n"
                                          "  entries:\n    locus-codegraph:\n      settings:\n        drift_guard: true\n"
                                          "        max_files: 2500\nterminal:\n  backend: local\n")
        path = os.environ.get("PATH", os.defpath)
        os.environ.clear()
        os.environ.update({"PATH": path, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
                           "TERMINAL_ENV": "local", "PYTHONDONTWRITEBYTECODE": "1"})
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(args.source))
        from agent.runtime_cwd import set_session_cwd
        from hermes_cli import lifecycle, plugins
        from model_tools import handle_function_call
        plugins.discover_plugins()
        active = next(row for row in plugins.get_plugin_manager().list_plugins() if row["name"] == "locus-codegraph")
        assert active["enabled"] and not active["error"], active
        previous = Path.cwd()
        try:
            for case in cases:
                workspace = root / case["case"]
                for filename, content in case["files"].items():
                    dest = workspace / filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)
                os.chdir(workspace)
                set_session_cwd(str(workspace))
                os.environ.update({"TERMINAL_CWD": str(workspace), "LOCUS_WORKSPACE": str(workspace)})
                lifecycle.invoke_hook("on_session_start", session_id=case["case"])
                payload = {"mode": "patch", "patch": patch_text(case["helper"], case["files"][case["helper"]], case["after"])}
                result = handle_function_call("patch", payload, session_id=case["case"], task_id=case["case"],
                                              turn_id="edit", api_request_id="edit", tool_call_id=case["case"])
                result = json.loads(result) if isinstance(result, str) else result
                assert result.get("success"), result
                assert (workspace / case["helper"]).read_text() == case["after"]
                warning = result.get("locus_drift_alert") or ""
                named = sorted(set(re.findall(r"Update caller `([^`]+:\d+)`", warning)))
                row = {key: value for key, value in case.items() if key not in {"files", "after"}}
                row.update({"patch_args": payload, "patch_result": result, "named_sites": named,
                            "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in case["files"].items()}})
                # Paths only; the run has no credential environment.
                encoded = json.dumps(row).replace(str(root), "<scratch>")
                report["cases"].append(json.loads(encoded))
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(case["case"], "broken", sum(label["error"] is not None for label in case["labels"].values()),
                      "named", len(named), flush=True)
        finally:
            os.chdir(previous)
    report["summary"] = score(report["cases"])
    report["status"] = "complete"
    report["distinct_labelled_sites"] = len({(row["group"] if row["group"] == "pinned-real-source" else row["fixture_name"], site)
                                             for row in report["cases"] for site in row["labels"]})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    if args.check_cross_file:
        assert all(group["named_compatible"] == group["unlabelled_named"] == 0
                   and not any(site["scope"] == "cross-file" for site in group["missed_sites"])
                   for group in report["summary"].values()), report["summary"]


if __name__ == "__main__":
    main()
