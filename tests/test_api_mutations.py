"""Mutation API input validation and workspace isolation regressions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard import plugin_api
from locus_core import get_hotspots_report, index_workspace, reindex_edited_file, open_locus_db, workspace_id_for


class TestApiMutations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "first"
        self.workspace.mkdir()
        self.other_workspace = root / "second"
        self.other_workspace.mkdir()
        env = patch.dict(os.environ, {"LOCUS_DB_PATH": str(root / "api.db")})
        env.start()
        self.addCleanup(env.stop)
        notify = patch.object(plugin_api, "_notify_desktop")
        self.notify = notify.start()
        self.addCleanup(notify.stop)
        app = FastAPI()
        app.include_router(plugin_api.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def make_alert(self) -> int:
        (self.workspace / "lib.py").write_text("def calculate(value):\n    return value\n")
        (self.workspace / "app.py").write_text("from lib import calculate\ndef run():\n    return calculate(1)\n")
        index_workspace(self.workspace)
        (self.workspace / "lib.py").write_text("def calculate(value, factor):\n    return value * factor\n")
        reindex_edited_file(self.workspace, "lib.py")
        return get_hotspots_report(self.workspace)["open_drift_alerts"][0]["id"]

    def test_rejects_invalid_alert_ids_without_mutation(self) -> None:
        alert_id = self.make_alert()
        self.assertEqual(alert_id, 1)
        for value in (None, "oops", "1", True, False, 0, -1, 1.5, 1 << 100, [], {}):
            with self.subTest(value=value):
                response = self.client.post("/resolve-drift", json={"alert_id": value})
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(get_hotspots_report(self.workspace)["open_drift_alerts"]), 1)
        self.notify.assert_not_called()

    def test_nonexistent_or_wrong_workspace_alert_is_not_successful(self) -> None:
        alert_id = self.make_alert()
        for payload in (
            {"alert_id": alert_id + 1},
            {"alert_id": alert_id, "workspace": str(self.other_workspace)},
        ):
            response = self.client.post("/resolve-drift", json=payload)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(len(get_hotspots_report(self.workspace)["open_drift_alerts"]), 1)

    def test_dismissal_is_scoped_idempotent_and_refreshes_desktop(self) -> None:
        alert_id = self.make_alert()
        payload = {"alert_id": alert_id, "workspace": str(self.workspace)}
        for _ in range(2):
            response = self.client.post("/resolve-drift", json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["resolved_id"], alert_id)
        self.assertEqual(get_hotspots_report(self.workspace)["open_drift_alerts"], [])
        self.notify.assert_called_with(self.workspace.resolve(), "drift_dismissed")

    def test_reindex_rejects_truthy_strings_and_non_string_workspaces(self) -> None:
        for payload in (
            {"workspace": str(self.workspace), "force": "false"},
            {"workspace": str(self.workspace), "force": 1},
            {"workspace": str(self.workspace), "force": None},
            {"workspace": [str(self.workspace)]},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/reindex", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/reindex", json={"workspace": str(self.workspace), "force": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])

    def test_summary_counts_all_alerts_and_bounds_history_without_crossing_workspaces(self) -> None:
        alert_id = self.make_alert()
        ws_id = workspace_id_for(self.workspace)
        with open_locus_db() as conn:
            for i in range(24):
                conn.execute(
                    """INSERT INTO drift_alerts (workspace_id, created_at, edited_path, symbol_name,
                       severity, old_signature, new_signature, reason, affected_callers_json)
                       SELECT workspace_id, created_at, edited_path, ?, severity, old_signature,
                              new_signature, reason, affected_callers_json FROM drift_alerts WHERE id = ?""",
                    (f"calculate_{i}", alert_id),
                )
        data = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(data["open_drift_count"], 25)
        self.assertEqual(len(data["drift_alerts"]), 20)
        self.assertTrue(data["drift_alerts_truncated"])
        with open_locus_db() as conn:
            conn.execute("UPDATE drift_alerts SET resolved = 1 WHERE workspace_id = ?", (ws_id,))
        data = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(data["open_drift_count"], 0)
        self.assertFalse(data["drift_alerts_truncated"])
        self.assertEqual(data["closed_drift_count"], 25)
        self.assertEqual(len(data["closed_drift_alerts"]), 10)
        other = self.client.get("/summary", params={"workspace": str(self.other_workspace)}).json()
        self.assertEqual(other["closed_drift_count"], 0)
        self.assertEqual(other["closed_drift_alerts"], [])

    def test_graph_query_limits_reject_out_of_bounds_values(self) -> None:
        for route, params in (
            ("/graph", {"max_nodes": 100000}),
            ("/graph", {"max_nodes": 0}),
            ("/impact", {"target": "lib.py", "max_depth": 6}),
            ("/symbols", {"limit": -1}),
            ("/symbols", {"limit": 100000}),
        ):
            response = self.client.get(route, params=params)
            self.assertEqual(response.status_code, 422, response.text)

    def test_rescan_exposes_parse_diagnostics_without_erasing_valid_graph(self) -> None:
        (self.workspace / "lib.py").write_text("def calculate(value):\n    return value\n")
        index_workspace(self.workspace)
        (self.workspace / "lib.py").write_text("def calculate(value\n")
        response = self.client.post("/reindex", json={"workspace": str(self.workspace), "force": True})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["file_count"], 1)
        self.assertEqual(data["parse_errors"][0]["path"], "lib.py")
        self.assertIn("line", data["parse_errors"][0]["error"])
        # A fresh page reads this persistent warning without relying on POST state.
        refreshed = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(refreshed["scan_status"]["parse_errors"][0]["path"], "lib.py")
        self.assertGreater(refreshed["scan_status"]["last_indexed_at"], 0)
        (self.workspace / "lib.py").write_text("def calculate(value):\n    return value\n")
        reindex_edited_file(self.workspace, "lib.py")
        repaired = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(repaired["scan_status"]["parse_errors"], [])

    def test_summary_keeps_file_limit_warning_after_refresh(self) -> None:
        for name in ("a.py", "b.py"):
            (self.workspace / name).write_text("value = 1\n")
        index_workspace(self.workspace, max_files=1)
        first = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        second = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        for data in (first, second):
            self.assertTrue(data["scan_status"]["truncated"])
            self.assertEqual(data["scan_status"]["max_files"], 1)

    def test_dashboard_rescan_preserves_the_existing_scan_cap(self) -> None:
        (self.workspace / "lib.py").write_text("value = 1\n")
        for cap in (1, 20000):
            with self.subTest(cap=cap):
                index_workspace(self.workspace, max_files=cap)
                with patch.object(plugin_api, "index_workspace", wraps=index_workspace) as scan:
                    response = self.client.post("/reindex", json={"workspace": str(self.workspace), "force": True})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(scan.call_args.kwargs["max_files"], cap)
                self.assertEqual(response.json()["max_files"], cap)

    def test_skipped_caller_warning_and_open_alert_survive_api_refresh_until_rechecked(self) -> None:
        alert_id = self.make_alert()
        caller = self.workspace / "app.py"
        broken = caller.read_text()
        caller.write_text(broken + "#" + "x" * (512 * 1024) + "\n")
        response = self.client.post("/reindex", json={"workspace": str(self.workspace), "force": True})
        self.assertEqual(response.status_code, 200, response.text)
        for _ in range(2):
            summary = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
            self.assertEqual(summary["open_drift_count"], 1)
            self.assertEqual(summary["closed_drift_count"], 0)
            self.assertEqual(summary["drift_alerts"][0]["id"], alert_id)
            retained = summary["drift_alerts"][0]
            self.assertEqual(retained["severity"], "warning")
            self.assertEqual(retained["affected_callers"][0]["analysis_status"], "needs_verification")
            self.assertNotIn("suggestion", retained["affected_callers"][0])
            stale = summary["scan_status"]["stale_files"]
            self.assertEqual([item["path"] for item in stale], ["app.py"])
            self.assertTrue(stale[0]["reason"])

        # Becoming readable again clears only the coverage warning, not the
        # still-incompatible caller. Only an actual supported repair clears it.
        caller.write_text(broken)
        reindex_edited_file(self.workspace, "app.py")
        checked = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(checked["scan_status"]["stale_files"], [])
        self.assertEqual(checked["open_drift_count"], 1)
        self.assertEqual(checked["drift_alerts"][0]["severity"], "critical")
        caller.write_text(broken.replace("calculate(1)", "calculate(1, 2)"))
        reindex_edited_file(self.workspace, "app.py")
        repaired = self.client.get("/summary", params={"workspace": str(self.workspace)}).json()
        self.assertEqual(repaired["open_drift_count"], 0)
        self.assertEqual(repaired["closed_drift_count"], 1)


if __name__ == "__main__":
    unittest.main()
