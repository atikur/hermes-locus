"""Adversarial checks for evaluation validity, independent of the Locus implementation."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from evaluate_refactor_feedback import aggregate, audit_pair_results, fingerprint, randomized_jobs, stop_reason, verify_source
from evaluation_boundary import check_call
from summarize_refactor_feedback import summarize


class EvaluationProtocolTests(unittest.TestCase):
    def test_summary_does_not_turn_interruption_into_comparative_failure(self):
        rows = [{"task": "example", "repeat": repeat, "warning_enabled": on,
                 "pass": on, "cli_exit": 0 if repeat == 1 or on else 124,
                 "model_exit": 0 if repeat == 1 or on else None,
                 "tokens": {"total": 100} if repeat == 1 or on else None}
                for repeat in (1, 2) for on in (True, False)]
        data = {"model": "test", "plugin_engine_sha256": "test", "reports": rows,
                "schedule": [[row["task"], row["repeat"], row["warning_enabled"]] for row in rows],
                "pair_input_checks": [{"task": "example", "repeat": repeat,
                                       "same_patch_result_except_warning": True} for repeat in (1, 2)]}
        result = summarize(data, {"interrupted_attempt": {"task": "example", "repeat": 2, "warning_enabled": False}})
        self.assertEqual(result["recorded_attempts"], 4)
        self.assertEqual(result["completed_sessions"], 3)
        self.assertEqual(result["complete_matched_pairs"], 1)
        self.assertEqual(result["matched_outcomes"]["on_only_pass"], 1)
        self.assertEqual(result["all_recorded_usage"]["sessions_with_token_accounting"], 3)
        self.assertEqual(result["all_recorded_usage"]["reported_tokens"]["total"], 300)
        self.assertIsNone(result["all_recorded_usage"]["trace_reported_cost_fields"])

    def test_file_boundary_blocks_private_paths_and_symlink_escapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary)
            root = outer / "public"
            root.mkdir()
            (outer / "private-plugin.py").write_text("private marker")
            (root / "escape").symlink_to(outer, target_is_directory=True)
            for tool, args in [
                ("read_file", {"path": "../private-plugin.py"}),
                ("read_file", {"path": str(outer / "private-plugin.py")}),
                ("read_file", {"path": "escape/private-plugin.py"}),
                ("search_files", {"path": "escape", "pattern": "private"}),
                ("search_files", {"path": ". ../private-plugin.py", "pattern": "private"}),
                ("search_files", {"path": ".,../private-plugin.py", "pattern": "private"}),
                ("search_files", {"path": ". " + str(outer), "pattern": "private"}),
                ("write_file", {"path": "../private-plugin.py", "content": "bad"}),
                ("patch", {"path": "../private-plugin.py", "old_string": "private", "new_string": "bad"}),
                ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: good.txt\n+ok\n*** Update File: ../private-plugin.py\n@@\n-private\n+bad\n*** End Patch"}),
                ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: good.txt\n*** Move to: ../private-plugin.py\n@@\n-ok\n+bad\n*** End Patch"}),
                ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Add File: good.txt\n+ok\n***Update File: ../private-plugin.py\n@@\n-old\n+bad\n*** End Patch"}),
                ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Move File: good.txt -> ../private-plugin.py\n*** End Patch"}),
                ("terminal", {"command": "cat ../private-plugin.py"}),
                ("read_file", {"path": None}),
            ]:
                self.assertEqual(check_call(tool, args, root)["action"], "block", (tool, args))
            for tool, args in [
                ("read_file", {"path": "agent/source.py"}),
                ("search_files", {"pattern": "public"}),
                ("search_files", {"path": "agent, tests", "pattern": "public"}),
                ("write_file", {"path": "new/file.py", "content": "public"}),
                ("patch", {"path": "agent/source.py"}),
                ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: agent/source.py\n@@\n-a\n+b\n*** End Patch"}),
            ]:
                self.assertIsNone(check_call(tool, args, root), (tool, args))

    def test_stops_depend_on_usage_or_infrastructure_not_success(self):
        failures = [{"pass": False, "tokens": {"total": 100}}] * 3
        self.assertIsNone(stop_reason(failures, 500, 500, 2))
        self.assertEqual(stop_reason(failures, 500, 250, 2), "total_token_limit")
        self.assertEqual(stop_reason([{"tokens": {"total": 501}}], 500, 1000, 2), "session_token_anomaly")
        self.assertEqual(stop_reason([{"infrastructure_error": "Timeout"}, {"provider_infrastructure_error": "quota"}],
                                     500, 1000, 2), "repeated_infrastructure_errors")
        self.assertIsNone(stop_reason([{"infrastructure_error": "Timeout"}, {"pass": False}], 500, 1000, 2))

    def test_schedule_is_reproducible_balanced_and_paired(self):
        jobs = randomized_jobs(["a", "b", "c"], 4, 17)
        self.assertEqual(jobs, randomized_jobs(["a", "b", "c"], 4, 17))
        self.assertNotEqual(jobs, randomized_jobs(["a", "b", "c"], 4, 18))
        self.assertEqual(len(set(jobs)), 24)
        for index in range(0, len(jobs), 2):
            first, second = jobs[index:index + 2]
            self.assertEqual(first[:2], second[:2])
            self.assertNotEqual(first[2], second[2])
        for name in ("a", "b", "c"):
            self.assertEqual(sum(job[0] == name and job[2] for job in jobs[::2]), 2)

    def test_infrastructure_failures_stay_in_denominator_and_missing_usage_is_not_zero(self):
        output = aggregate([
            {"warning_enabled": True, "pass": True, "tokens": {"total": 100}, "reported_cost_fields": {"cost_usd": 0.25}},
            {"warning_enabled": True, "pass": False, "infrastructure_error": "TimeoutError"},
            {"warning_enabled": False, "pass": False, "tokens": {"total": 80}},
        ])
        arm = output["warning_on"]
        self.assertEqual((arm["runs"], arm["passed"], arm["infrastructure_failures"]), (2, 1, 1))
        self.assertEqual(arm["runs_with_token_accounting"], 1)
        self.assertEqual(arm["tokens"]["total"], 100)
        self.assertEqual(arm["reported_cost_fields"], {"cost_usd": 0.25})
        self.assertEqual(output["warning_off"]["reported_cost_fields"], {})

    def test_pair_control_rejects_non_warning_input_difference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for arm, value in (("on", {"success": True, "output": "OK\n\nwarn", "locus_drift_alert": "warn"}),
                               ("off", {"success": True, "output": "OK"})):
                path = root / f"example-1-warning-{arm}"
                path.mkdir()
                (path / "helper-edit-result.json").write_text(json.dumps(value))
            rows = [{"task": "example", "repeat": 1}]
            self.assertTrue(audit_pair_results(root, rows)[0]["same_patch_result_except_warning"])
            (root / "example-1-warning-off/helper-edit-result.json").write_text('{"success":false,"output":"OK"}')
            self.assertFalse(audit_pair_results(root, rows)[0]["same_patch_result_except_warning"])

    def test_source_oracle_rejects_weakened_assertion_unrelated_edit_and_wrong_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            baseline, workspace = root / "base", root / "work"
            baseline.mkdir()
            (baseline / "helper.py").write_text("def calculate(value, *, limit):\n    return value + limit\n")
            (baseline / "test_helper.py").write_text("from helper import calculate\ndef test_result():\n    assert calculate(2, limit=3) == 5\n")
            snapshot = fingerprint(baseline)
            task = {"helper": "helper.py", "symbol": "calculate", "old": "limit", "new": "ceiling",
                    "sites": ["test_helper.py:3"]}
            shutil.copytree(baseline, workspace)
            for path in workspace.iterdir():
                path.write_text(path.read_text().replace("limit", "ceiling"))
            good = verify_source(task, baseline, workspace, snapshot)
            self.assertTrue(good["expected_files_only"] and good["helper_rename_ok"])
            self.assertEqual(good["verified_caller_sites"], task["sites"])
            self.assertEqual(set(good["repaired_call_binding_errors"].values()), {None})
            test = workspace / "test_helper.py"
            correct = test.read_text()
            test.write_text(correct.replace("assert calculate(2, ceiling=3) == 5", "calculate(2, ceiling=3)"))
            self.assertEqual(verify_source(task, baseline, workspace, snapshot)["verified_caller_sites"], [])
            test.write_text(correct)
            (workspace / "unrelated.txt").write_text("unexpected")
            self.assertFalse(verify_source(task, baseline, workspace, snapshot)["expected_files_only"])
            (workspace / "unrelated.txt").unlink()
            helper = workspace / "helper.py"
            helper.write_text(helper.read_text().replace("value + ceiling", "value - ceiling"))
            self.assertFalse(verify_source(task, baseline, workspace, snapshot)["helper_rename_ok"])


if __name__ == "__main__":
    unittest.main()
