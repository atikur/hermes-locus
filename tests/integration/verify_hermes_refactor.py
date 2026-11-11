#!/usr/bin/env python3
"""Run one side of a controlled refactor in a disposable Hermes source checkout.

Use a Hermes runtime Python and a separate, unchanged Hermes source checkout.
Run with --variant baseline, then --variant candidate. Both dispatch the same
patch and repair through Hermes and run the original upstream display tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Any


WORKSPACE_COMMIT = "78abaafcccec20a546c94e8734a70e38c08a6a69"
BASELINE_COMMIT = "ab90659627b4b0427c847e04d1a9156994346103"
PLUGIN_NAME = "locus-codegraph"
TEST_FILE = "tests/agent/test_display.py"
CALL_SITES = (
    "gateway/platforms/base.py:2014",
    "gateway/run_turn_progress.py:375",
    f"{TEST_FILE}:142",
    f"{TEST_FILE}:155",
    f"{TEST_FILE}:164",
)

RENAME = """*** Begin Patch
*** Update File: agent/display.py
@@
-def prepare_tool_preview(tool_name: str, args: dict | None, *, fallback: str, max_len: int) -> ToolPreview:
+def prepare_tool_preview(tool_name: str, args: dict | None, *, fallback: str, limit: int) -> ToolPreview:
@@
-    text = _tail_trunc(full_text, max_len)
+    text = _tail_trunc(full_text, limit)
*** End Patch"""

REPAIR = """*** Begin Patch
*** Update File: gateway/platforms/base.py
@@
         prepared = prepare_tool_preview(
-            event.tool_name, event.args, fallback=event.preview, max_len=cap)
+            event.tool_name, event.args, fallback=event.preview, limit=cap)
*** Update File: gateway/run_turn_progress.py
@@
-        prepared = prepare_tool_preview(tool_name, args, fallback=preview, max_len=self._preview_cap())
+        prepared = prepare_tool_preview(tool_name, args, fallback=preview, limit=self._preview_cap())
*** Update File: tests/agent/test_display.py
@@
             fallback=url[:17] + "...",
-            max_len=20,
+            limit=20,
@@
-            "browser_navigate", None, fallback=url, max_len=40
+            "browser_navigate", None, fallback=url, limit=40
@@
             fallback="how to parse a URL",
-            max_len=12,
+            limit=12,
*** End Patch"""


def extract_commit(repository: Path, revision: str, target: Path) -> None:
    target.mkdir()
    with tempfile.TemporaryFile() as archive:
        subprocess.run(["git", "-C", str(repository), "archive", revision], stdout=archive, check=True)
        archive.seek(0)
        with tarfile.open(fileobj=archive) as contents:
            contents.extractall(target, filter="data")


def git_head(repository: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()


def engine_digest(plugin: Path) -> str:
    paths = [plugin / "__init__.py", *sorted((plugin / "locus_core").glob("*.py"))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(plugin).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="unchanged Hermes Git checkout")
    parser.add_argument("--runtime", type=Path, required=True, help="Hermes runtime directory")
    parser.add_argument("--pytest-deps", type=Path, required=True, help="directory containing pytest and pytest-asyncio")
    parser.add_argument("--variant", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--install-from-git", action="store_true",
        help="use Hermes to install and enable the exact committed candidate in the scratch profile",
    )
    args = parser.parse_args()
    if args.install_from_git and args.variant != "candidate":
        parser.error("--install-from-git requires --variant candidate")
    source, runtime, deps, output = (p.resolve() for p in (args.source, args.runtime, args.pytest_deps, args.output))
    plugin = Path(__file__).resolve().parents[2]
    plugin_revision = "candidate working tree"
    if args.variant == "baseline":
        plugin_revision = BASELINE_COMMIT
    elif args.install_from_git:
        plugin_revision = git_head(plugin)
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence: dict[str, Any] = {
        "scenario": "Rename the required keyword-only argument of prepare_tool_preview at the same arity",
        "variant": args.variant,
        "workspace_commit": WORKSPACE_COMMIT,
        "hermes_host_commit": git_head(source),
        "plugin_revision": plugin_revision,
        "python_version": platform.python_version(),
        "model_calls": 0,
        "max_files": 20000,
        "host_hook_timeout_seconds": 120,
        "steps": [],
        "status": "RUNNING",
    }

    with tempfile.TemporaryDirectory(prefix="locus-hermes-refactor-") as scratch:
        root = Path(scratch).resolve()
        workspace, home = root / "workspace", root / "hermes-home"
        installed = home / "plugins" / PLUGIN_NAME
        installed.parent.mkdir(parents=True)
        replacements = (
            (workspace, "<workspace>"), (home, "<isolated-hermes-home>"),
            (root, "<scratch>"), (source, "<host-source>"), (runtime, "<runtime>"),
            (deps, "<pytest-deps>"), (plugin, "<plugin-source>"),
            (Path(sys.executable), "<hermes-python>"), (Path.home(), "<user-home>"),
        )

        def save() -> None:
            encoded = json.dumps(evidence, indent=2, ensure_ascii=False)
            for path, label in replacements:
                encoded = encoded.replace(json.dumps(str(path), ensure_ascii=False)[1:-1], label)
            output.write_text(encoded + "\n", encoding="utf-8")

        try:
            extract_commit(source, WORKSPACE_COMMIT, workspace)
            if args.variant == "baseline":
                extract_commit(plugin, BASELINE_COMMIT, installed)
            elif not args.install_from_git:
                shutil.copytree(plugin, installed, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.pyc"))

            enabled = "[]" if args.install_from_git else f"[{PLUGIN_NAME}]"
            (home / "config.yaml").write_text(
                f"plugins:\n  enabled: {enabled}\n  hook_callback_timeout: 120\n"
                "  entries:\n    locus-codegraph:\n      settings:\n"
                "        auto_index_on_start: true\n        drift_guard: true\n        max_files: 20000\n"
                "terminal:\n  backend: local\n  cwd: " + json.dumps(str(workspace)) + "\n",
                encoding="utf-8",
            )
            system_env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
                          if key in os.environ}
            system_env["PATH"] = os.environ.get("PATH", os.defpath)
            os.environ.clear()
            os.environ.update({
                **system_env, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(runtime),
                "TERMINAL_CWD": str(workspace), "LOCUS_WORKSPACE": str(workspace), "TERMINAL_ENV": "local",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            if args.install_from_git:
                command = ["-m", "hermes_cli.main", "plugins", "install", plugin.as_uri(),
                           "--ref", plugin_revision, "--enable"]
                result = subprocess.run(
                    [sys.executable, *command], cwd=source, text=True, capture_output=True, timeout=300,
                    env={**os.environ, "PYTHONPATH": str(source), "GIT_TERMINAL_PROMPT": "0"},
                )
                evidence["install"] = {
                    "command": ["<hermes-python>", *command], "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                }
                if result.returncode != 0:
                    raise RuntimeError(f"Hermes plugin install exited {result.returncode}. See the replay report.")
                installed_revision = git_head(installed)
                evidence["install"]["installed_revision"] = installed_revision
                if installed_revision != plugin_revision:
                    raise RuntimeError(f"Expected plugin {plugin_revision}, installed {installed_revision}")
                print(f"Installed and enabled Locus at {installed_revision}", flush=True)
            evidence["plugin_engine_sha256"] = engine_digest(installed)

            sys.dont_write_bytecode = True
            sys.path.insert(0, str(source))
            os.chdir(workspace)

            from agent.runtime_cwd import set_session_cwd
            from hermes_cli import lifecycle, plugins
            from model_tools import handle_function_call

            set_session_cwd(str(workspace))
            plugins.discover_plugins()
            active = next(p for p in plugins.get_plugin_manager().list_plugins() if p["name"] == PLUGIN_NAME)
            assert active["enabled"] and not active["error"], active
            assert active["tools"] == 5 and active["hooks"] == 3, active
            evidence["plugin_discovery"] = active
            index_started = time.perf_counter()
            lifecycle.invoke_hook("on_session_start", session_id="locus-refactor-proof")
            index_elapsed_ms = round((time.perf_counter() - index_started) * 1000, 2)
            db = home / "plugin-data" / PLUGIN_NAME / "locus.db"
            with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT file_count, symbol_count, edge_count FROM workspaces").fetchone()
                assert row is not None, "session-start hook did not index the source"
                evidence["index"] = dict(zip(("files", "symbols", "edges"), row))
                evidence["index"]["elapsed_ms"] = index_elapsed_ms
                calls = conn.execute(
                    "SELECT caller_path, line_no FROM calls "
                    "WHERE callee_name = 'prepare_tool_preview' AND resolved_path = 'agent/display.py'"
                ).fetchall()
                indexed = {f"{path}:{line}" for path, line in calls}
                assert set(CALL_SITES) <= indexed, indexed
                evidence["indexed_call_sites"] = sorted(indexed)
            save()

            def call(name: str, payload: dict[str, Any], label: str) -> dict[str, Any]:
                sequence = len(evidence["steps"]) + 1
                started = time.perf_counter()
                raw = handle_function_call(
                    name, payload, task_id="locus-refactor-proof", session_id="locus-refactor-proof",
                    turn_id="refactor-proof", api_request_id=f"request-{sequence}", tool_call_id=f"tool-{sequence}",
                )
                result = json.loads(raw) if isinstance(raw, str) else raw
                evidence["steps"].append({
                    "label": label, "tool": name, "args": payload, "result": result,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                })
                save()
                assert isinstance(result, dict), raw
                assert not result.get("error") and result.get("success") is not False and result.get("ok") is not False, result
                print(label, flush=True)
                return result

            def run_tests(label: str, expected_exit: int) -> str:
                # The focused display tests need only their own fixtures. Skip
                # the repo-wide session cleanup, which enumerates host processes.
                command = ["-m", "pytest", "-q", "--tb=short", "--noconftest", "-p", "pytest_asyncio.plugin", TEST_FILE]
                env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(workspace), str(deps))),
                       "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
                result = subprocess.run([sys.executable, *command], env=env, cwd=workspace,
                                        text=True, capture_output=True, timeout=90)
                evidence["steps"].append({
                    "label": label, "command": ["<hermes-python>", *command], "exit_code": result.returncode,
                    "stdout": result.stdout, "stderr": result.stderr,
                })
                save()
                assert result.returncode == expected_exit, result.stdout + result.stderr
                print(label, result.stdout.strip().splitlines()[-1], flush=True)
                return result.stdout

            run_tests("upstream tests before the edit", 0)
            assert call("locus_hotspots", {}, "alerts before the edit")["open_drift_count"] == 0
            broken = call("patch", {"mode": "patch", "patch": RENAME}, "rename the required keyword")
            warning = broken.get("locus_drift_alert", "")
            if args.variant == "candidate":
                assert "[Locus Signature Drift Alert]" in warning, broken
                assert all(site in warning for site in CALL_SITES), warning
            else:
                assert not warning, broken
            alerts = call("locus_hotspots", {}, "alerts after the rename")["open_drift_count"]
            assert alerts == (1 if args.variant == "candidate" else 0), alerts

            failing = run_tests("upstream tests with broken callers", 1)
            assert "3 failed" in failing and "unexpected keyword argument 'max_len'" in failing, failing
            repaired = call("patch", {"mode": "patch", "patch": REPAIR}, "repair all five callers")
            assert not repaired.get("locus_drift_alert"), repaired
            assert call("locus_hotspots", {}, "alerts after repair")["open_drift_count"] == 0
            run_tests("upstream tests after repair", 0)
            evidence["status"] = "PASS"
        except Exception as exc:
            evidence["status"] = "FAIL"
            evidence["error"] = str(exc)
            raise
        finally:
            save()


if __name__ == "__main__":
    main()
