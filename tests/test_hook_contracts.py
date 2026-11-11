"""Regression coverage for Hermes' actual patch payloads and hook dispatch order."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tests.test_drift_and_hooks import _FakePluginContext, _load_plugin_root_module
except ImportError:  # unittest discover -s tests
    from test_drift_and_hooks import _FakePluginContext, _load_plugin_root_module


class TestHookContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {
            "LOCUS_DB_PATH": str(Path(self.tmp.name) / "locus.db"),
            "HERMES_HOME": str(Path(self.tmp.name) / "hermes-home"),
        }).start()
        self.plugin = _load_plugin_root_module()
        patch.object(self.plugin, "_broadcast_desktop_update").start()
        self.ctx = _FakePluginContext(str(self.workspace))
        self.plugin.register(self.ctx)
        (self.workspace / "lib.py").write_text("def calc(value):\n    return value\n")
        (self.workspace / "app.py").write_text("from lib import calc\ndef run():\n    return calc(1)\n")
        self.ctx.hooks["on_session_start"](session_id="s1")

    def break_signature(self) -> None:
        (self.workspace / "lib.py").write_text("def calc(value, factor):\n    return value * factor\n")

    def call(self, args=None, result=None, **extra):
        return {
            "tool_name": "patch",
            "args": args if args is not None else {"path": "lib.py"},
            "result": result if result is not None else json.dumps({"success": True, "files_modified": [str(self.workspace / "lib.py")]}),
            "task_id": "task-1", "session_id": "s1", "turn_id": "turn-1", "tool_call_id": "call-1", **extra,
        }

    def test_transform_before_post_injects_once_and_indexes_once(self) -> None:
        self.break_signature()
        call = self.call()
        with patch.object(self.plugin, "reindex_edited_files", wraps=self.plugin.reindex_edited_files) as reindex:
            result = self.ctx.hooks["transform_tool_result"](**call)
            self.ctx.hooks["post_tool_call"](**call)
            repeated = self.ctx.hooks["transform_tool_result"](**call)
        self.assertIn("[Locus Signature Drift Alert]", json.loads(result)["locus_drift_alert"])
        self.assertEqual(reindex.call_count, 1)
        self.assertIsNone(repeated)

    def test_patch_only_payload_indexes_all_files_and_removes_transient_alert(self) -> None:
        self.break_signature()
        (self.workspace / "app.py").write_text("from lib import calc\ndef run():\n    return calc(1, 2)\n")
        args = {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: lib.py\n@@\n*** Update File: app.py\n@@\n*** End Patch"}
        call = self.call(args=args, result=json.dumps({"success": True}))
        with patch.object(self.plugin, "reindex_edited_files", wraps=self.plugin.reindex_edited_files) as reindex:
            self.ctx.hooks["post_tool_call"](**call)
            result = self.ctx.hooks["transform_tool_result"](**call)
        self.assertEqual(reindex.call_count, 1)
        self.assertIsNone(result)
        self.assertEqual(self.plugin.get_hotspots_report(self.workspace)["open_drift_alerts"], [])

    def test_result_paths_override_argument_path(self) -> None:
        self.break_signature()
        result = self.ctx.hooks["transform_tool_result"](**self.call(args={"path": "wrong-cwd/lib.py"}))
        self.assertIn("app.py:3", json.loads(result)["locus_drift_alert"])

    def test_structured_result_preserves_json_envelope(self) -> None:
        self.break_signature()
        raw = {"success": True, "output": "Patched lib.py", "files_modified": ["lib.py"]}
        output = self.ctx.hooks["transform_tool_result"](**self.call(result=raw))
        decoded = json.loads(output)
        self.assertTrue(decoded["success"])
        self.assertEqual(decoded["files_modified"], ["lib.py"])
        self.assertIn("Patched lib.py", decoded["output"])
        self.assertIn("Locus Signature Drift Alert", decoded["output"])
        self.assertNotIn("locus_drift_alert", raw)

    def test_partial_patch_failure_inspects_only_successful_result_paths(self) -> None:
        self.break_signature()
        result = {"success": False, "error": "Apply phase failed", "files_modified": ["lib.py"]}
        args = {"mode": "patch", "patch": "*** Update File: lib.py\n*** Update File: untouched.py"}
        with patch.object(self.plugin, "reindex_edited_files", wraps=self.plugin.reindex_edited_files) as reindex:
            output = self.ctx.hooks["transform_tool_result"](**self.call(args=args, result=json.dumps(result), status="error"))
        self.assertIn("Locus Signature Drift Alert", output)
        self.assertEqual(reindex.call_count, 1)

    def test_failed_and_cancelled_calls_do_not_index(self) -> None:
        for status, result in (("error", {"success": False}), ("cancelled", {"files_modified": ["lib.py"]})):
            with self.subTest(status=status), patch.object(self.plugin, "reindex_edited_files") as reindex:
                self.ctx.hooks["post_tool_call"](**self.call(result=result, status=status))
                self.assertIsNone(self.ctx.hooks["transform_tool_result"](**self.call(result=result, status=status)))
                reindex.assert_not_called()

    def test_patch_moves_and_deletes_extract_both_endpoints(self) -> None:
        args = {"patch": "*** Begin Patch\n*** Move File: old.py -> new.py\n*** Delete File: gone.py\n*** End Patch"}
        self.assertEqual(self.plugin._edited_paths(args, {}), ["old.py", "new.py", "gone.py"])

    def test_invalid_python_preserves_graph_and_returns_separate_index_warning(self) -> None:
        self.break_signature()
        self.ctx.hooks["transform_tool_result"](**self.call())
        (self.workspace / "lib.py").write_text("def calc(value, factor\n")
        call = self.call(tool_call_id="invalid-edit", result=json.dumps({"success": True, "output": "Patched lib.py"}))
        self.ctx.hooks["post_tool_call"](**call)
        transformed = self.ctx.hooks["transform_tool_result"](**call)
        decoded = json.loads(transformed)
        self.assertTrue(decoded["success"])
        self.assertEqual(decoded["locus_index_warnings"][0]["path"], "lib.py")
        self.assertIn("[Locus Indexing Warning]", decoded["output"])
        self.assertIn("WARNING", decoded["locus_drift_alert"])
        self.assertIn("Inspect `app.py:3`", decoded["locus_drift_alert"])
        self.assertNotIn("Update caller", decoded["locus_drift_alert"])
        alerts = self.plugin.get_hotspots_report(self.workspace)["open_drift_alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["severity"], "warning")
        self.assertEqual(alerts[0]["affected_callers"][0]["analysis_status"], "needs_verification")
        self.assertNotIn("suggestion", alerts[0]["affected_callers"][0])
        self.assertIn("factor", self.plugin.lookup_symbol_details(self.workspace, "calc")["symbol"]["signature"])
        self.break_signature()
        recovered = self.ctx.hooks["transform_tool_result"](**self.call(tool_call_id="recovered-edit"))
        self.assertIn("CRITICAL", json.loads(recovered)["locus_drift_alert"])
        self.assertEqual(self.plugin.get_hotspots_report(self.workspace)["open_drift_alerts"][0]["severity"], "critical")

    def test_multi_file_patch_reports_drift_and_parse_diagnostics_together(self) -> None:
        self.break_signature()
        (self.workspace / "new.py").write_text("def invalid(\n")
        call = self.call(result=json.dumps({"success": True, "files_modified": ["lib.py", "new.py"]}))
        transformed = self.ctx.hooks["transform_tool_result"](**call)
        decoded = json.loads(transformed)
        self.assertIn("app.py:3", decoded["locus_drift_alert"])
        self.assertEqual(decoded["locus_index_warnings"][0]["path"], "new.py")

    def test_oversized_edit_warns_even_without_an_existing_caller_alert(self) -> None:
        caller = self.workspace / "app.py"
        caller.write_text(caller.read_text() + "#" + "x" * (512 * 1024) + "\n")
        call = self.call(args={"path": "app.py"}, result={"success": True, "output": "Patched app.py",
                                                        "files_modified": ["app.py"]})
        transformed = self.ctx.hooks["transform_tool_result"](**call)
        decoded = json.loads(transformed)
        self.assertTrue(decoded["success"])
        self.assertEqual(decoded["locus_index_warnings"][0]["path"], "app.py")
        self.assertIn("last-valid", decoded["locus_index_warnings"][0]["error"])
        self.assertIn("Resolve the reported indexing issue", decoded["output"])
        self.assertEqual(self.plugin.get_hotspots_report(self.workspace)["open_drift_alerts"], [])

    def test_real_hermes_dispatch_with_suppressed_post_hook(self) -> None:
        try:
            import model_tools
            from hermes_cli import lifecycle
        except ImportError:
            self.skipTest("Hermes runtime not installed")

        args = {"path": "lib.py", "old_string": "def calc(value):", "new_string": "def calc(value, factor):"}
        raw = json.dumps({"success": True, "files_modified": [str(self.workspace / "lib.py")]})
        observed = []

        def invoke(name, **kwargs):
            observed.append(name)
            return [self.ctx.hooks[name](**kwargs)]

        def execute(*args, **kwargs):
            self.break_signature()
            return raw

        with patch.object(lifecycle, "has_hook", side_effect=lambda name: name in self.ctx.hooks), \
             patch.object(lifecycle, "invoke_hook", side_effect=invoke), \
             patch.object(model_tools, "_execute_tool", side_effect=execute), \
             patch.object(model_tools, "_pre_dispatch_guards", return_value=(args, None)), \
             patch.object(self.plugin, "reindex_edited_files", wraps=self.plugin.reindex_edited_files) as reindex:
            # This is the sequential registry path in agent/tool_executor.py:
            # dispatch transforms under suppression, then the executor emits post.
            with model_tools.suppress_post_tool_call_hook():
                output = model_tools.handle_function_call(
                    "patch", args, task_id="task-1", session_id="s1", tool_call_id="call-1", turn_id="turn-1",
                    skip_pre_tool_call_hook=True, skip_tool_request_middleware=True, skip_tool_execution_middleware=True,
                )
            model_tools._emit_post_tool_call_hook(
                function_name="patch", function_args=args, result=output,
                task_id="task-1", session_id="s1", tool_call_id="call-1", turn_id="turn-1",
            )
        self.assertEqual(observed, ["transform_tool_result", "post_tool_call"])
        self.assertEqual(reindex.call_count, 1)
        self.assertIn("app.py:3", json.loads(output)["locus_drift_alert"])


if __name__ == "__main__":
    unittest.main()
