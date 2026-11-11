"""Partial Python edits must not become proof of removed or repaired contracts."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file
from locus_core.storage import open_locus_db, workspace_id_for


class TestInvalidPythonIndexing(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "graph.db"
        self.write("lib.py", "def calculate(value, factor=1):\n    return value * factor\n")
        self.write("app.py", "from lib import calculate\nresult = calculate(3)\n")
        index_workspace(self.root, db_path=self.db)

    def write(self, path: str, source: str) -> None:
        (self.root / path).write_text(source, encoding="utf-8")

    def alerts(self) -> list[dict]:
        return get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]

    def break_contract(self) -> int:
        self.write("lib.py", "def calculate(value, factor):\n    return value * factor\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        return self.alerts()[0]["id"]

    def test_invalid_callee_preserves_baseline_without_removed_export_alert(self) -> None:
        self.write("lib.py", "def calculate(value, factor:\n")
        result = reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(self.alerts(), [])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "parse_error")
        self.assertIn("line 1", result["error"])
        # Repairing syntax still compares against the last valid optional parameter.
        self.break_contract()
        self.assertEqual(self.alerts()[0]["old_signature"], "def calculate(value, factor = 1)")

    def test_invalid_caller_does_not_clear_drift_and_valid_repair_does(self) -> None:
        original_id = self.break_contract()
        self.write("app.py", "from lib import calculate\nresult = calculate(\n")
        reindex_edited_file(self.root, "app.py", db_path=self.db)
        self.assertEqual([a["id"] for a in self.alerts()], [original_id])
        self.write("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        reindex_edited_file(self.root, "app.py", db_path=self.db)
        self.assertEqual(self.alerts(), [])

    def test_scan_preserves_invalid_caller_and_retries_after_repair(self) -> None:
        original_id = self.break_contract()
        self.write("app.py", "from lib import calculate\nresult = calculate(\n")
        result = index_workspace(self.root, force=True, db_path=self.db)
        self.assertEqual([a["id"] for a in self.alerts()], [original_id])
        self.assertEqual(result["parse_errors"][0]["path"], "app.py")
        self.write("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        index_workspace(self.root, db_path=self.db)
        self.assertEqual(self.alerts(), [])

    def test_new_invalid_file_is_reported_but_not_counted_as_indexed(self) -> None:
        self.write("broken.py", "def incomplete(\n")
        result = index_workspace(self.root, db_path=self.db)
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["parse_errors"][0]["path"], "broken.py")
        with open_locus_db(db_path=self.db) as conn:
            paths = {row["rel_path"] for row in conn.execute(
                "SELECT rel_path FROM files WHERE workspace_id = ?",
                (workspace_id_for(self.root),),
            )}
        self.assertNotIn("broken.py", paths)

    def test_first_edit_reports_parse_failure_when_no_baseline_exists(self) -> None:
        self.write("broken.py", "def incomplete(\n")
        result = reindex_edited_file(
            self.root, "broken.py", db_path=Path(self.tmp.name) / "fresh.db",
        )
        self.assertTrue(result["initial_scan"])
        self.assertEqual(result["reason"], "parse_error")
        self.assertIn("line 1", result["error"])


if __name__ == "__main__":
    unittest.main()
