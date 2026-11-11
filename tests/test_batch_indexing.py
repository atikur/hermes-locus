"""Atomic multi-file edit behavior, complete caller persistence, and partial failures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from locus_core import get_hotspots_report, index_workspace, reindex_edited_files
from locus_core import indexer
from locus_core.storage import open_locus_db


class BatchIndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"
        self.write("lib.py", "def prepare(old): pass\n")
        self.write("caller.py", "from lib import prepare\nprepare(old=1)\n")
        index_workspace(self.root, db_path=self.db)

    def write(self, path, content):
        (self.root / path).write_text(content)

    def edit(self, paths):
        return reindex_edited_files(self.root, paths, db_path=self.db)

    def test_patch_definition_and_caller_relinks_once_without_transient_alert(self):
        self.write("lib.py", "def prepare(new): pass\n")
        self.write("caller.py", "from lib import prepare\nprepare(new=1)\n")
        with patch.object(indexer, "_relink_and_rank", wraps=indexer._relink_and_rank) as relink, \
             patch.object(indexer, "reconcile_drift_alerts", wraps=indexer.reconcile_drift_alerts) as reconcile:
            result = self.edit(["caller.py", "lib.py", self.root / "lib.py"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["edited_paths"], ["caller.py", "lib.py"])
        self.assertEqual(len(result["outcomes"]), 2)
        self.assertEqual(relink.call_count, 1)
        self.assertEqual(reconcile.call_count, 1)
        self.assertEqual(result["drift_alerts"], [])
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_mixed_delete_parse_failure_and_repair_keeps_last_valid_graph(self):
        self.write("other.py", "def work(value): pass\n")
        self.write("user.py", "from other import work\nwork(1)\n")
        index_workspace(self.root, db_path=self.db)
        self.write("lib.py", "def prepare(new): pass\n")
        self.edit(["lib.py"])
        self.write("caller.py", "from lib import prepare\nprepare(new=1)\n")
        self.write("lib.py", "def prepare(\n")
        (self.root / "other.py").unlink()
        with patch.object(indexer, "_relink_and_rank", wraps=indexer._relink_and_rank) as relink:
            result = self.edit(["other.py", "lib.py", "caller.py"])
        self.assertEqual(relink.call_count, 1)
        self.assertEqual(result["parse_errors"][0]["path"], "lib.py")
        self.assertEqual([a["symbol_name"] for a in result["drift_alerts"]], ["prepare", "work"])
        self.assertEqual(result["drift_alerts"][0]["severity"], "warning")
        with open_locus_db(self.db) as conn:
            signature = conn.execute("SELECT signature FROM symbols WHERE rel_path='lib.py'").fetchone()[0]
            status = json.loads(conn.execute("SELECT scan_status_json FROM workspaces").fetchone()[0])
        self.assertIn("new", signature)
        self.assertEqual(status["parse_errors"][0]["path"], "lib.py")
        self.write("lib.py", "def prepare(new): pass\n")
        self.write("user.py", "pass\n")
        repaired = self.edit(["lib.py", "user.py"])
        self.assertEqual(repaired["drift_alerts"], [])
        with open_locus_db(self.db) as conn:
            status = json.loads(conn.execute("SELECT scan_status_json FROM workspaces").fetchone()[0])
        self.assertEqual(status["parse_errors"], [])
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_two_definition_changes_keep_old_baselines_for_every_file(self):
        self.write("caller.py", "pass\n")
        self.write("other.py", "from lib import prepare\ndef invoke(value):\n    prepare(old=value)\n")
        self.write("user.py", "from other import invoke\ninvoke(value=1)\n")
        index_workspace(self.root, db_path=self.db)
        self.write("lib.py", "def prepare(new): pass\n")
        self.write("other.py", "from lib import prepare\ndef invoke(new_value):\n    prepare(new=new_value)\n")
        result = self.edit(["other.py", "lib.py"])
        self.assertEqual([a["symbol_name"] for a in result["drift_alerts"]], ["invoke"])
        self.assertEqual(result["drift_alerts"][0]["affected_callers"][0]["path"], "user.py")

    def test_all_callers_are_persisted_beyond_display_limits(self):
        self.write("caller.py", "from lib import prepare\n" + "prepare(old=1)\n" * 40)
        index_workspace(self.root, db_path=self.db)
        self.write("lib.py", "def prepare(new): pass\n")
        result = self.edit(["lib.py"])
        self.assertEqual(len(result["drift_alerts"][0]["affected_callers"]), 40)
        with open_locus_db(self.db) as conn:
            callers = json.loads(conn.execute("SELECT affected_callers_json FROM drift_alerts WHERE resolved=0").fetchone()[0])
        self.assertEqual(len(callers), 40)
        # Repair only the first 39 sites. A site outside the old storage cap
        # must remain visible and keep the alert open.
        self.write("caller.py", "from lib import prepare\n" + "prepare(new=1)\n" * 39 + "prepare(old=1)\n")
        result = self.edit(["caller.py"])
        self.assertEqual([c["line"] for c in result["drift_alerts"][0]["affected_callers"]], [41])

    def test_invalid_and_outside_paths_never_relink_or_replace_sources(self):
        self.write("lib.py", "def prepare(\n")
        with patch.object(indexer, "_relink_and_rank", wraps=indexer._relink_and_rank) as relink:
            result = self.edit(["../outside.py", "lib.py", "notes.txt"])
        relink.assert_not_called()
        self.assertEqual(result["edited_paths"], [])
        self.assertEqual(result["parse_errors"][0]["path"], "lib.py")
        self.assertEqual([o["reason"] for o in result["outcomes"]],
                         ["outside_workspace", "parse_error", "unsupported_extension"])

    def test_capped_rescan_keeps_unscanned_callers_alerts_and_parse_errors(self):
        # Sort the definition first so the caller falls outside a capped scan.
        (self.root / "lib.py").rename(self.root / "aaa_lib.py")
        (self.root / "caller.py").unlink()
        self.write("zzz_app.py", "from aaa_lib import prepare\nprepare(old=1)\n")
        index_workspace(self.root, db_path=self.db)
        self.write("aaa_lib.py", "def prepare(new): pass\n")
        self.edit(["aaa_lib.py"])
        self.write("zzz_app.py", "def invalid(\n")
        self.edit(["zzz_app.py"])
        scan = index_workspace(self.root, force=True, max_files=1, db_path=self.db)
        self.assertTrue(scan["truncated"])
        self.assertEqual(scan["removed_files"], 0)
        self.assertEqual(scan["file_count"], 2)
        self.assertEqual(scan["parse_errors"][0]["path"], "zzz_app.py")
        alerts = get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["affected_callers"][0]["path"], "zzz_app.py")
