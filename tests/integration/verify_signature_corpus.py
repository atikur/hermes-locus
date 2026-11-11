#!/usr/bin/env python3
"""Compare real Hermes edit results on pinned Python source projections.

Run with the Hermes Python interpreter. Each case starts from the original
selected files. Only the edited definition is changed. CPython signature
binding labels the existing call expressions without importing their packages.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import inspect
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from contextlib import closing
from pathlib import Path


PLUGIN = "locus-codegraph"
BASELINE = "ab90659627b4b0427c847e04d1a9156994346103"
SOURCES = {
    "hermes": ("NousResearch/hermes-agent", "78abaafcccec20a546c94e8734a70e38c08a6a69", {
        "agent/display.py": "9ac45f1e331ec34b0165d196532fcc5d9ef5768f",
        "gateway/platforms/base.py": "c4a8cecfd7d3698e9a6428f1a4aa8ef9d4fd1918",
        "gateway/run_turn_progress.py": "d04ec27574c13adcebda7f61661dd787086a35b0",
        "tests/agent/test_display.py": "afd74c461303e3c5df60e336a5d7fb828fdad76a",
    }),
    "httpx": ("encode/httpx", "26d48e0634e6ee9cdc0533996db289ce4b430177", {
        "httpx/_models.py": "67d74bf86bfc80e22d9a4a3153572845accd9039",
        "httpx/_utils.py": "7fe827da4d071b32ea6da44328629699d6fc88ce",
    }),
    "requests": ("psf/requests", "b25c87d7cb8d6a18a37fa12442b5f883f9e41741", {
        "src/requests/__init__.py": "051cda1340effaa0706b46dd68ac002ceda3d45c",
        "src/requests/api.py": "5960744552e7f8eea815429e7bdad38b0cc2741d",
        "src/requests/sessions.py": "731550de88aceb59747460faf519c2277e300e87",
        "src/requests/utils.py": "8ab55852cc2188c53f08462ec4319c97fa49f04b",
        "tests/test_lowlevel.py": "859d07e8a594b2a80acae666949971f6ead90299",
        "tests/test_utils.py": "f9a287af1b95922741705c538ef4c5a6034bfd05",
    }),
}

# Case edits are independent changes to the pinned definition. Caller locations
# were identified from the pinned sources and are checked before dispatch.
TARGETS = [
    ("hermes", "agent/display.py", "prepare_tool_preview", None, (
        "gateway/platforms/base.py:2014", "gateway/run_turn_progress.py:375",
        "tests/agent/test_display.py:142", "tests/agent/test_display.py:155", "tests/agent/test_display.py:164",
    ), {
        "keyword-rename": [("max_len: int", "limit: int"), ("_tail_trunc(full_text, max_len)", "_tail_trunc(full_text, limit)")],
        "required-keyword": [("max_len: int)", "max_len: int, trace: bool)")],
        "positional-only": [("*, fallback: str, max_len: int", "max_len: int, /, *, fallback: str")],
        "optional-keyword": [("max_len: int)", "max_len: int, trace: bool = False)")],
        "positional-rename": [("tool_name: str", "tool: str"), ("build_tool_preview(tool_name,", "build_tool_preview(tool,")],
        "body-only": [("    text = _tail_trunc(full_text, max_len)",
                       "    cap = max_len\n    text = _tail_trunc(full_text, cap)")],
    }),
    ("httpx", "httpx/_utils.py", "to_bytes_or_str", None, ("httpx/_models.py:135",), {
        "keyword-rename": [("match_type_of: typing.AnyStr", "match: typing.AnyStr"),
                           ("isinstance(match_type_of, str)", "isinstance(match, str)")],
        "required-keyword": [("match_type_of: typing.AnyStr)", "match_type_of: typing.AnyStr, *, trace: bool)")],
        "positional-only": [("match_type_of: typing.AnyStr)", "match_type_of: typing.AnyStr, /)")],
        "optional-keyword": [("match_type_of: typing.AnyStr)", "match_type_of: typing.AnyStr, *, trace: bool = False)")],
        "positional-rename": [("value: str", "text: str"), ("return value if", "return text if"),
                              ("else value.encode()", "else text.encode()")],
        "body-only": [("    return value if isinstance(match_type_of, str) else value.encode()",
                       "    result = value if isinstance(match_type_of, str) else value.encode()\n    return result")],
    }),
    ("requests", "src/requests/utils.py", "get_environ_proxies", None, (
        "src/requests/sessions.py:760", "tests/test_utils.py:232", "tests/test_utils.py:243",
        "tests/test_utils.py:255", "tests/test_utils.py:272",
    ), {
        "keyword-rename": [("no_proxy=None", "exclude=None"), ("no_proxy=no_proxy", "no_proxy=exclude")],
        "required-keyword": [("no_proxy=None)", "no_proxy=None, *, trace)")],
        "positional-only": [("no_proxy=None)", "no_proxy=None, /)")],
        "optional-keyword": [("no_proxy=None)", "no_proxy=None, *, trace=False)")],
        "positional-rename": [("(url, no_proxy=None)", "(target, no_proxy=None)"),
                              ("should_bypass_proxies(url,", "should_bypass_proxies(target,")],
        "body-only": [("        return getproxies()", "        proxies = getproxies()\n        return proxies")],
    }),
    ("requests", "src/requests/api.py", "get", "requests", tuple(
        f"tests/test_lowlevel.py:{line}" for line in (59, 123, 181, 231, 268, 299, 332, 349, 393, 422)
    ), {
        "unsupported-attribute": [("def get(url, params=None, **kwargs):", "def get(*, url, params=None, **kwargs):")],
    }),
]


def git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repository), *args])


def pinned_sources(host: Path, cache: Path | None) -> dict[str, dict[str, bytes]]:
    all_files = {}
    for key, (repo, commit, files) in SOURCES.items():
        source_files = {}
        for path, blob in files.items():
            if key == "hermes":
                raw = git(host, "show", f"{commit}:{path}")
            elif cache is not None:
                raw = (cache / key / path).read_bytes()
            else:
                url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
                with urllib.request.urlopen(url, timeout=20) as response:
                    raw = response.read()
            assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == blob, (repo, path)
            source_files[path] = raw
        all_files[key] = source_files
    return all_files


def definition(text: str, name: str) -> ast.FunctionDef:
    definitions = [node for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(definitions) == 1, name
    return definitions[0]


def callable_signature(text: str, name: str) -> inspect.Signature:
    # Execute a no-op function with the source's parameter layout. Preserve
    # whether defaults exist while avoiding annotations/default expressions
    # and never executing the source function or importing its package.
    stub = definition(text, name)
    stub.decorator_list = []
    stub.returns = None
    for item in ast.walk(stub.args):
        if isinstance(item, ast.arg):
            item.annotation = None
    stub.args.defaults = [ast.Constant(None) for _ in stub.args.defaults]
    stub.args.kw_defaults = [ast.Constant(None) if item is not None else None for item in stub.args.kw_defaults]
    stub.body = [ast.Pass()]
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[stub], type_ignores=[])), "<signature>", "exec"), namespace)
    return inspect.signature(namespace[name])


def revised_source(source: str, name: str, edits: list[tuple[str, str]]) -> str:
    node = definition(source, name)
    lines = source.splitlines(keepends=True)
    start, end = node.lineno - 1, node.end_lineno
    body = "".join(lines[start:end])
    for before, after in edits:
        assert body.count(before) == 1, (name, before)
        body = body.replace(before, after)
    return "".join(lines[:start]) + body + "".join(lines[end:])


def checked_calls(files: dict[str, bytes], callee: str, name: str, receiver: str | None) -> dict[str, ast.Call]:
    found = {}
    for path, source in files.items():
        if path == callee:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            if receiver is None:
                matches = isinstance(node.func, ast.Name) and node.func.id == name
            else:
                matches = (isinstance(node.func, ast.Attribute) and node.func.attr == name
                           and isinstance(node.func.value, ast.Name) and node.func.value.id == receiver)
            if matches:
                location = f"{path}:{node.lineno}"
                assert location not in found, location
                found[location] = node
    return found


def binding_error(signature: inspect.Signature, call: ast.Call) -> str | None:
    assert all(not isinstance(arg, ast.Starred) for arg in call.args)
    assert all(keyword.arg is not None for keyword in call.keywords)
    args = [None] * len(call.args)
    kwargs = {keyword.arg: None for keyword in call.keywords}
    try:
        signature.bind(*args, **kwargs)
    except TypeError as error:
        return str(error)
    return None


def patch_text(path: str, before: str, after: str) -> str:
    diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))[2:]
    body = "\n".join("@@" if line.startswith("@@ ") else line for line in diff)
    return f"*** Begin Patch\n*** Update File: {path}\n{body}\n*** End Patch"


def run_variant(args: argparse.Namespace) -> dict:
    plugin = Path(__file__).resolve().parents[2]
    revision = BASELINE if args.variant == "baseline" else args.candidate_ref
    commit = git(plugin, "rev-parse", revision).decode().strip()
    sources = pinned_sources(args.source, args.cache)
    with tempfile.TemporaryDirectory(prefix="locus-signature-corpus-") as folder:
        root = Path(folder).resolve()
        home = root / "hermes-home"
        install = home / "plugins" / PLUGIN
        install.mkdir(parents=True)
        with tempfile.TemporaryFile() as archive:
            subprocess.run(["git", "-C", str(plugin), "archive", commit], stdout=archive, check=True)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as contents:
                contents.extractall(install, filter="data")
        (home / "config.yaml").write_text(
            f"plugins:\n  enabled: [{PLUGIN}]\n  hook_callback_timeout: 120\n"
            f"  entries:\n    {PLUGIN}:\n      settings:\n"
            "        auto_index_on_start: true\n        drift_guard: true\n        max_files: 2500\n"
            "terminal:\n  backend: local\n",
            encoding="utf-8",
        )
        command_path = os.environ.get("PATH", os.defpath)
        os.environ.clear()
        os.environ.update({
            "PATH": command_path, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
            "TERMINAL_ENV": "local", "PYTHONDONTWRITEBYTECODE": "1",
        })
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(args.source))
        from agent.runtime_cwd import set_session_cwd
        from hermes_cli import lifecycle, plugins
        from model_tools import handle_function_call

        plugins.discover_plugins()
        active = next(entry for entry in plugins.get_plugin_manager().list_plugins() if entry["name"] == PLUGIN)
        assert active["enabled"] and not active["error"] and active["hooks"] == 3, active
        results = []
        original_cwd = Path.cwd()
        try:
            for repo, callee, name, receiver, sites, mutations in TARGETS:
                files = sources[repo]
                before = files[callee].decode("utf-8")
                old_signature = callable_signature(before, name)
                calls = checked_calls(files, callee, name, receiver)
                assert set(calls) == set(sites), (repo, name, sorted(calls))
                assert all(binding_error(old_signature, call) is None for call in calls.values()), (repo, name)
                for kind, edits in mutations.items():
                    identifier = f"{repo}-{name}-{kind}"
                    workspace = root / identifier
                    for path, raw in files.items():
                        dest = workspace / path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(raw)
                    after = revised_source(before, name, edits)
                    new_signature = callable_signature(after, name)
                    labels = {site: binding_error(new_signature, calls[site]) for site in sites}
                    os.environ.update({"TERMINAL_CWD": str(workspace), "LOCUS_WORKSPACE": str(workspace)})
                    os.chdir(workspace)
                    set_session_cwd(str(workspace))
                    lifecycle.invoke_hook("on_session_start", session_id=identifier)
                    database = home / "plugin-data" / PLUGIN / "locus.db"
                    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
                        indexed = conn.execute("SELECT file_count FROM workspaces WHERE root_path = ?", (str(workspace),)).fetchone()
                    assert indexed == (len(files),), (identifier, indexed)
                    payload = {"mode": "patch", "patch": patch_text(callee, before, after)}
                    raw = handle_function_call("patch", payload, task_id=identifier, session_id=identifier,
                                               turn_id=identifier, api_request_id=identifier, tool_call_id=identifier)
                    result = json.loads(raw) if isinstance(raw, str) else raw
                    assert result.get("success") and not result.get("error"), (identifier, result)
                    assert (workspace / callee).read_text(encoding="utf-8") == after, identifier
                    warning = result.get("locus_drift_alert", "")
                    named = sorted(set(re.findall(r"`([^`\n]+\.py:\d+)`", warning)))
                    results.append({
                        "case": identifier, "repository": repo, "kind": kind, "callee": f"{callee}:{definition(before, name).lineno}",
                        "binding_scope": "module attribute outside precise binding scope" if receiver else "direct import",
                        "old_signature": str(old_signature), "new_signature": str(new_signature), "oracle_errors": labels,
                        "named_sites": named, "patch_args": payload, "patch_result": result,
                    })
                    print(args.variant, identifier, "labelled breaks", sum(error is not None for error in labels.values()),
                          "named", len(named), flush=True)
        finally:
            os.chdir(original_cwd)
        output = {"plugin_commit": commit, "plugin_version": active["version"], "cases": results}
        encoded = json.dumps(output, ensure_ascii=False)
        for path, label in ((root, "<scratch>"), (args.source, "<host>"), (args.runtime, "<runtime>"), (Path.home(), "<user-home>")):
            encoded = encoded.replace(json.dumps(str(path), ensure_ascii=False)[1:-1], label)
        return json.loads(encoded)


def summarize(cases: list[dict], include_unsupported: bool) -> dict:
    counts = dict(cases=0, broken_cases=0, fully_named_broken_cases=0, compatible_cases=0,
                  compatible_warned_cases=0, broken=0, compatible=0, named_broken=0,
                  missed_broken=0, named_compatible=0, unlabelled_named=0)
    for case in cases:
        if not include_unsupported and case["kind"] == "unsupported-attribute":
            continue
        labels, named = case["oracle_errors"], set(case["named_sites"])
        broken = {site for site, error in labels.items() if error is not None}
        compatible = set(labels) - broken
        counts["cases"] += 1
        if broken:
            counts["broken_cases"] += 1
            counts["fully_named_broken_cases"] += int(broken <= named)
        else:
            counts["compatible_cases"] += 1
            counts["compatible_warned_cases"] += int(bool(case["patch_result"].get("locus_drift_alert")))
        counts["broken"] += len(broken)
        counts["compatible"] += len(compatible)
        counts["named_broken"] += len(named & broken)
        counts["missed_broken"] += len(broken - named)
        counts["named_compatible"] += len(named & compatible)
        counts["unlabelled_named"] += len(named - labels.keys())
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="unchanged Hermes host Git checkout")
    parser.add_argument("--runtime", type=Path, required=True, help="Hermes runtime directory")
    parser.add_argument("--cache", type=Path, help="optional external source files under httpx/ and requests/; otherwise fetch pinned raw blobs")
    parser.add_argument("--candidate-ref", required=True, help="Git revision of the v0.1.4 plugin to evaluate")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check-supported", action="store_true", help="fail if the candidate misses supported calls or warns on compatible calls")
    parser.add_argument("--variant", choices=("both", "baseline", "candidate"), default="both", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.source, args.runtime, args.output = (path.resolve() for path in (args.source, args.runtime, args.output))
    args.cache = args.cache.resolve() if args.cache is not None else None
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.variant != "both":
        report = run_variant(args)
    else:
        report = {
            "method": "Matched real Hermes patch calls on selected unmodified files at pinned source revisions",
            "oracle": "CPython inspect.Signature.bind using actual argument kinds and keyword names from the pinned call AST",
            "limits": ["Targeted source projections, not a representative repository-wide accuracy estimate",
                       "Only call binding is labelled; runtime behavior, aliases and dynamic values are outside this oracle",
                       "The explicitly selected requests.get attribute case is included in the all-case counts"],
            "sources": {key: {"repo": repo, "commit": commit, "git_blob_sha1": files}
                        for key, (repo, commit, files) in SOURCES.items()},
            "host_commit": git(args.source, "rev-parse", "HEAD").decode().strip(),
            "python_version": platform.python_version(), "drift_guard": True, "model_calls": 0,
            "max_files": 2500, "host_hook_timeout_seconds": 120,
            "variants": {},
        }
        with tempfile.TemporaryDirectory(prefix="locus-signature-report-") as scratch:
            for variant in ("baseline", "candidate"):
                target = Path(scratch) / f"{variant}.json"
                subprocess.run([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:],
                                "--variant", variant, "--output", str(target)], check=True)
                result = json.loads(target.read_text(encoding="utf-8"))
                result["supported_cases"] = summarize(result["cases"], include_unsupported=False)
                result["all_cases"] = summarize(result["cases"], include_unsupported=True)
                report["variants"][variant] = result
        baseline, candidate = (report["variants"][key]["cases"] for key in ("baseline", "candidate"))
        fields = ("case", "oracle_errors", "patch_args")
        assert [[case[field] for field in fields] for case in baseline] == [[case[field] for field in fields] for case in candidate]
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.check_supported and args.variant == "both":
        supported = report["variants"]["candidate"]["supported_cases"]
        assert all(supported[key] == 0 for key in (
            "missed_broken", "named_compatible", "unlabelled_named", "compatible_warned_cases",
        )), supported


if __name__ == "__main__":
    main()
