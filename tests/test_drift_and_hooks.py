"""Unit tests for Signature Drift Guard and Hermes lifecycle hook wiring."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file  # noqa: E402


def _load_plugin_root_module():
    spec = importlib.util.spec_from_file_location(
        "locus_test_pkg",
        str(REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(REPO_ROOT)]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class _FakePluginContext:
    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self.state = _FakeState()
        self.tools: dict[str, Any] = {}
        self.hooks: dict[str, Any] = {}
        self.refs: list[Any] = []
        self.commands: dict[str, Any] = {}
        self.cli_commands: dict[str, Any] = {}
        self.skills: list[str] = []

    def session_cwd(self) -> str:
        return self._cwd

    def get_config(self, key: str, default: Any = None) -> Any:
        return default

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Any, **kwargs: Any) -> None:
        self.tools[name] = handler

    def register_hook(self, hook_name: str, callback: Any, *, hold_stream: bool = False) -> None:
        self.hooks[hook_name] = callback

    def register_context_reference(self, provider: Any) -> None:
        self.refs.append(provider)

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.commands[name] = handler

    def register_cli_command(self, name: str, **kwargs: Any) -> None:
        self.cli_commands[name] = kwargs

    def register_skill(self, name: str, path: Path, **kwargs: Any) -> None:
        self.skills.append(name)


class TestDriftAndHooks(unittest.TestCase):
    def test_signature_arity_drift_detection_and_auto_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()
            db_file = Path(tmp) / "drift.db"

            auth_py = ws / "auth.py"
            billing_py = ws / "billing.py"

            auth_py.write_text(
                "def verify_token(token: str) -> bool:\n    return bool(token)\n",
                encoding="utf-8",
            )
            billing_py.write_text(
                "from auth import verify_token\n\ndef charge(tok: str) -> bool:\n    return verify_token(tok)\n",
                encoding="utf-8",
            )

            index_workspace(ws, db_path=db_file)

            # Breaking edit: add required parameter `audience`
            auth_py.write_text(
                "def verify_token(token: str, audience: str) -> bool:\n    return bool(token and audience)\n",
                encoding="utf-8",
            )
            outcome = reindex_edited_file(ws, auth_py, tool_name="patch", db_path=db_file)
            self.assertTrue(outcome["ok"])
            self.assertEqual(len(outcome["drift_alerts"]), 1)
            alert = outcome["drift_alerts"][0]
            self.assertEqual(alert["severity"], "critical")
            self.assertEqual(alert["symbol_name"], "verify_token")
            self.assertEqual(alert["affected_callers"][0]["path"], "billing.py")
            self.assertEqual(alert["affected_callers"][0]["arg_count"], 1)

            # Now fix caller in billing.py -> drift alert auto-resolves!
            billing_py.write_text(
                "from auth import verify_token\n\ndef charge(tok: str) -> bool:\n    return verify_token(tok, 'payments')\n",
                encoding="utf-8",
            )
            reindex_edited_file(ws, billing_py, tool_name="patch", db_path=db_file)
            hotspots = get_hotspots_report(ws, db_path=db_file)
            self.assertEqual(len(hotspots["open_drift_alerts"]), 0)

    def test_deleted_symbol_drift_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            ws.mkdir()
            db_file = Path(tmp) / "del.db"

            (ws / "helpers.py").write_text("def slugify(text: str) -> str:\n    return text.lower()\n", encoding="utf-8")
            (ws / "views.py").write_text("from helpers import slugify\ndef show(): return slugify('Hi')\n", encoding="utf-8")
            index_workspace(ws, db_path=db_file)

            (ws / "helpers.py").write_text("def normalize(text: str) -> str:\n    return text.strip()\n", encoding="utf-8")
            out = reindex_edited_file(ws, "helpers.py", db_path=db_file)
            self.assertEqual(len(out["drift_alerts"]), 1)
            self.assertEqual(out["drift_alerts"][0]["new_signature"], "<removed>")

    def test_plugin_hooks_append_drift_banner_to_json_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            db_path = Path(tmp) / "hook.db"
            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_path)
            try:
                (ws / "lib.py").write_text("def calc(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
                (ws / "app.py").write_text("from lib import calc\ndef main():\n    return calc(5)\n", encoding="utf-8")

                plugin_mod = _load_plugin_root_module()
                ctx = _FakePluginContext(str(ws))
                plugin_mod.register(ctx)

                self.assertEqual(
                    set(ctx.tools.keys()),
                    {"locus_map", "locus_symbol", "locus_impact", "locus_hotspots", "locus_rehearse"},
                )
                self.assertEqual(
                    set(ctx.hooks.keys()),
                    {"on_session_start", "post_tool_call", "transform_tool_result"},
                )

                # Initial session index
                ctx.hooks["on_session_start"](session_id="s1")

                # Simulate breaking patch on lib.py
                (ws / "lib.py").write_text("def calc(x: int, factor: int) -> int:\n    return x * factor\n", encoding="utf-8")
                raw_result = json.dumps({"status": "ok", "output": "Patched lib.py"})

                ctx.hooks["post_tool_call"](
                    tool_name="patch",
                    args={"path": "lib.py"},
                    result=raw_result,
                    task_id="t1",
                    tool_call_id="call_42",
                )
                transformed = ctx.hooks["transform_tool_result"](
                    tool_name="patch",
                    args={"path": "lib.py"},
                    result=raw_result,
                    task_id="t1",
                    tool_call_id="call_42",
                )
                self.assertIsNotNone(transformed)
                parsed = json.loads(transformed)
                self.assertEqual(parsed["status"], "ok")
                self.assertIn("[Locus Signature Drift Alert]", parsed["output"])
                self.assertIn("app.py:3", parsed["locus_drift_alert"])
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env


if __name__ == "__main__":
    unittest.main()
