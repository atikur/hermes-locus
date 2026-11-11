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
from locus_core import get_hotspots_report, index_workspace, reindex_edited_file


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


if __name__ == "__main__":
    unittest.main()
