"""Tests for dashboard/plugin_api.py FastAPI endpoints and Hermes catalog validation."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.plugin_api import router as locus_router  # noqa: E402


class TestApiAndValidation(unittest.TestCase):
    def test_fastapi_plugin_routes_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "demo_repo"
            ws.mkdir()
            db_path = Path(tmp) / "api.db"
            (ws / "engine.py").write_text("def run_job(job_id: str) -> bool:\n    return True\n", encoding="utf-8")
            (ws / "worker.py").write_text(
                "from engine import run_job\ndef tick():\n    return run_job('j1')\n",
                encoding="utf-8",
            )

            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_path)
            try:
                app = FastAPI()
                app.include_router(locus_router, prefix="/api/plugins/locus-codegraph")
                client = TestClient(app)

                # 1. Reindex workspace
                r_scan = client.post("/api/plugins/locus-codegraph/reindex", json={"workspace": str(ws), "force": True})
                self.assertEqual(r_scan.status_code, 200)
                self.assertEqual(r_scan.json()["file_count"], 2)

                # 2. Summary defaults to most recently indexed workspace
                r_sum = client.get("/api/plugins/locus-codegraph/summary")
                self.assertEqual(r_sum.status_code, 200)
                self.assertEqual(r_sum.json()["file_count"], 2)

                # 3. Visual SVG graph with blast-radius focus
                r_graph = client.get("/api/plugins/locus-codegraph/graph", params={"focus": "engine.py"})
                self.assertEqual(r_graph.status_code, 200)
                gdata = r_graph.json()
                self.assertEqual(len(gdata["nodes"]), 2)
                self.assertIsNotNone(gdata["focus_impact"])

                # 4. Impact, symbol, symbols, hotspots
                self.assertEqual(
                    client.get("/api/plugins/locus-codegraph/impact", params={"target": "engine.py"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/api/plugins/locus-codegraph/symbol", params={"name": "run_job"}).status_code,
                    200,
                )
                self.assertEqual(
                    client.get("/api/plugins/locus-codegraph/symbols", params={"q": "run"}).status_code,
                    200,
                )
                self.assertEqual(client.get("/api/plugins/locus-codegraph/hotspots").status_code, 200)
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env

    def test_hermes_admission_validator_zero_warnings_when_available(self) -> None:
        try:
            from hermes_cli.plugin_validate import validate_plugin_dir
        except ImportError:
            self.skipTest("hermes_cli not on PYTHONPATH in standalone test run")

        report = validate_plugin_dir(REPO_ROOT)
        self.assertTrue(report.ok, f"Validation failed: {report.failures}")
        self.assertEqual(report.warnings, [], f"Unexpected warnings: {report.warnings}")


if __name__ == "__main__":
    unittest.main()
