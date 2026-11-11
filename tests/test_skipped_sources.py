"""Excluded source is unavailable evidence, not proof that callers were repaired."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from locus_core import get_hotspots_report, index_workspace, lookup_symbol_details, reindex_edited_file, reindex_edited_files
from locus_core.indexer import DEFAULT_MAX_FILE_BYTES
from locus_core.storage import open_locus_db


class SkippedSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"
        self.original = "from lib import calculate\ndef run():\n    return calculate(old=1)\n"
        (self.root / "lib.py").write_text("def calculate(old): return old\n")
        (self.root / "app.py").write_text(self.original)
        self.scan()
        (self.root / "lib.py").write_text("def calculate(new): return new\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(self.open_count(), 1)

    def scan(self):
        return index_workspace(self.root, force=True, db_path=self.db)

    def open_count(self):
        return get_hotspots_report(self.root, db_path=self.db)["open_drift_count"]

    def assert_uncertain(self):
        alert = get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"][0]
        self.assertEqual(alert["severity"], "warning")
        self.assertEqual(alert["affected_callers"][0]["analysis_status"], "needs_verification")

    def status(self):
        with open_locus_db(self.db) as conn:
            return json.loads(conn.execute("SELECT scan_status_json FROM workspaces").fetchone()[0])

    def oversize(self):
        (self.root / "app.py").write_text(self.original + "# padding\n" * (DEFAULT_MAX_FILE_BYTES // 10 + 2))

    def test_oversized_caller_retains_alert_until_successful_recheck(self):
        self.oversize()
        scan = self.scan()
        self.assertEqual(scan["removed_files"], 0)
        self.assertEqual(scan["file_count"], 2)
        self.assertEqual(self.open_count(), 1)
        self.assert_uncertain()
        self.assertEqual(scan["stale_files"][0]["path"], "app.py")
        self.assertIn("512 KiB", scan["stale_files"][0]["reason"])
        self.assertEqual(lookup_symbol_details(self.root, "app.py::run", db_path=self.db)["snippet"], "")
        # A cached rescan must not clear the warning or caller evidence.
        index_workspace(self.root, db_path=self.db)
        self.assertEqual(self.status()["stale_files"][0]["path"], "app.py")
        self.assertEqual(self.open_count(), 1)
        (self.root / "app.py").write_text(self.original.replace("old=", "new="))
        reindex_edited_file(self.root, "app.py", db_path=self.db)
        self.assertEqual(self.status()["stale_files"], [])
        self.assertEqual(self.open_count(), 0)

    def test_actual_deletion_removes_retained_evidence_and_stale_warning(self):
        self.oversize()
        self.scan()
        (self.root / "app.py").unlink()
        scan = self.scan()
        self.assertEqual(scan["removed_files"], 1)
        self.assertEqual(scan["stale_files"], [])
        self.assertEqual(self.open_count(), 0)

    def test_new_scan_exclusion_retains_alert_and_recovery_is_rechecked(self):
        (self.root / ".gitignore").write_text("app.py\n")
        scan = self.scan()
        self.assertEqual(scan["removed_files"], 0)
        self.assertIn("gitignored", self.status()["stale_files"][0]["reason"])
        self.assertEqual(self.open_count(), 1)
        (self.root / ".gitignore").write_text("")
        self.scan()
        self.assertEqual(self.status()["stale_files"], [])
        self.assertEqual(self.open_count(), 1)  # Rechecked source is still broken.

    def test_external_symlink_preserves_old_rows_without_reading_target_or_excerpt(self):
        external = Path(self.tmp.name) / "external.py"
        external.write_text("def run(): return 'EXTERNAL_SOURCE_MUST_NOT_BE_READ'\n")
        (self.root / "app.py").unlink()
        (self.root / "app.py").symlink_to(external)
        original_open = Path.open
        def guarded_open(path, *args, **kwargs):
            self.assertNotEqual(path.resolve(), external.resolve(), "Attempted to read external symlink target")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", guarded_open):
            self.scan()
            details = lookup_symbol_details(self.root, "app.py::run", db_path=self.db)
        self.assertEqual(details["snippet"], "")
        self.assertEqual(self.open_count(), 1)
        self.assertIn("Symbolic link", self.status()["stale_files"][0]["reason"])
        self.assertNotIn("EXTERNAL_SOURCE_MUST_NOT_BE_READ", json.dumps(details))

    def test_incremental_skip_records_warning_and_empty_file_proves_removal(self):
        self.oversize()
        result = reindex_edited_file(self.root, "app.py", db_path=self.db)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["stale_files"][0]["path"], "app.py")
        self.assertEqual(self.status()["stale_files"][0]["path"], "app.py")
        self.assertEqual(self.open_count(), 1)
        (self.root / "app.py").write_text("")
        self.scan()
        self.assertEqual(self.open_count(), 0)
        self.assertEqual(self.status()["stale_files"], [])

    def test_stale_caller_cannot_be_proven_repaired_by_restoring_callee(self):
        (self.root / "app.py").write_text(self.original.replace("old=", "new=") + "# padding\n" * (DEFAULT_MAX_FILE_BYTES // 10 + 2))
        self.scan()
        (self.root / "lib.py").write_text("def calculate(old): return old\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assert_uncertain()
        # Reading the actual caller now proves the still-broken new= binding.
        (self.root / "app.py").write_text(self.original.replace("old=", "new="))
        reindex_edited_file(self.root, "app.py", db_path=self.db)
        alert = get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"][0]
        self.assertEqual(alert["severity"], "critical")

    def test_stale_target_prevents_claiming_a_caller_repair_verified(self):
        (self.root / "lib.py").write_text("def calculate(new): return new\n" + "# padding\n" * (DEFAULT_MAX_FILE_BYTES // 10 + 2))
        self.scan()
        (self.root / "app.py").write_text(self.original.replace("old=", "new="))
        reindex_edited_file(self.root, "app.py", db_path=self.db)
        self.assert_uncertain()
        (self.root / "lib.py").write_text("def calculate(new): return new\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(self.open_count(), 0)
        self.assertEqual(self.status()["stale_files"], [])

    def test_batch_sees_new_skip_status_before_reconciling_other_edits(self):
        (self.root / "app.py").write_text(self.original.replace("old=", "new=") + "# padding\n" * (DEFAULT_MAX_FILE_BYTES // 10 + 2))
        (self.root / "lib.py").write_text("def calculate(old): return old\n")
        result = reindex_edited_files(self.root, ["app.py", "lib.py"], db_path=self.db)
        self.assertEqual(result["drift_alerts"][0]["severity"], "warning")
        self.assert_uncertain()

    def test_batch_only_returns_stale_warnings_for_current_prepared_paths(self):
        self.oversize()
        first = reindex_edited_files(self.root, ["app.py"], db_path=self.db)
        self.assertEqual(first["stale_files"][0]["path"], "app.py")
        second = reindex_edited_files(self.root, ["lib.py"], db_path=self.db)
        self.assertEqual(second["stale_files"], [])
        self.assertEqual(self.status()["stale_files"][0]["path"], "app.py")

    def test_capped_scan_cannot_prove_unvisited_caller_repaired(self):
        # The renamed caller sorts after lib.py and falls beyond the cap.
        (self.root / "app.py").rename(self.root / "zzz_app.py")
        self.scan()
        # Establish the new path's proven break before making it unavailable.
        (self.root / "lib.py").write_text("def calculate(old): return old\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        (self.root / "lib.py").write_text("def calculate(new): return new\n")
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        (self.root / "zzz_app.py").write_text(self.original.replace("old=", "new="))
        (self.root / "lib.py").write_text("def calculate(old): return old\n")
        index_workspace(self.root, max_files=1, force=True, db_path=self.db)
        self.assertEqual(self.status()["stale_files"][0]["path"], "zzz_app.py")
        self.assert_uncertain()
