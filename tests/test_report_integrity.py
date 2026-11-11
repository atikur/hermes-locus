"""Reports must not turn partial, ambiguous or bounded evidence into certainty."""
from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from locus_core import (
    analyze_impact, get_hotspots_report, get_repo_map, index_workspace,
    lookup_symbol_details, open_locus_db, workspace_id_for,
)
from locus_core.formatter import (
    format_drift_banner, format_hotspots_markdown, format_impact_markdown,
    format_repo_map_markdown, format_symbol_markdown,
)


class ReportIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.temp.name) / "index.db"

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)

    def test_ambiguous_targets_require_a_choice_and_confirmed_callers_stay_scoped(self):
        self.write("one/service.py", "def send(x): return x\n")
        self.write("two/service.py", "def send(x): return x\n")
        self.write("a.py", "from one.service import send\nsend(1)\n")
        self.write("b.py", "from two.service import send\nsend(2)\n")
        self.write("unknown.py", "def invoke(send):\n    return send(3)\n")
        index_workspace(self.root, db_path=self.db)
        ambiguous = analyze_impact(self.root, "service.py", db_path=self.db)
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["candidates"], ["one/service.py", "two/service.py"])
        ambiguous = lookup_symbol_details(self.root, "send", db_path=self.db)
        self.assertFalse(ambiguous["ok"])
        self.assertIn("one/service.py::send", format_symbol_markdown(ambiguous))
        symbol = lookup_symbol_details(self.root, "one/service.py::send", db_path=self.db)
        self.assertTrue(symbol["ok"])
        self.assertEqual([c["path"] for c in symbol["callers"]], ["a.py"])
        impact = analyze_impact(self.root, "one/service.py::send", db_path=self.db)
        self.assertEqual([c["caller_path"] for c in impact["external_call_sites"]], ["a.py"])

    def test_counts_and_target_alerts_are_not_limited_by_display_page(self):
        self.write("helper.py", "def f(): pass\n")
        index_workspace(self.root, db_path=self.db)
        ws = workspace_id_for(self.root)
        with open_locus_db(self.db) as conn:
            for i in range(25):
                conn.execute("""INSERT INTO drift_alerts
                    (workspace_id, created_at, edited_path, symbol_name, severity,
                     old_signature, new_signature, reason)
                    VALUES (?, ?, ?, ?, 'critical', 'f()', 'f(x)', 'missing argument')""",
                    (ws, i, "helper.py" if i == 0 else f"file{i}.py", "f"))
        self.assertEqual(get_repo_map(self.root, db_path=self.db)["open_drift_count"], 25)
        hotspots = get_hotspots_report(self.root, db_path=self.db)
        self.assertEqual(hotspots["open_drift_count"], 25)
        self.assertEqual(len(hotspots["open_drift_alerts"]), 20)
        self.assertIn("Showing 10 of 25", format_hotspots_markdown(hotspots))
        self.assertEqual(len(analyze_impact(self.root, "helper.py", db_path=self.db)["open_drift_alerts"]), 1)

    def test_exact_symbol_selector_is_not_limited_by_search_page(self):
        for i in range(120):
            self.write(f"module{i:03}.py", "def common(): return 1\n")
        index_workspace(self.root, db_path=self.db)
        result = lookup_symbol_details(self.root, "module119.py::common", db_path=self.db)
        self.assertTrue(result["ok"])
        self.assertEqual(result["symbol"]["rel_path"], "module119.py")

    def test_call_site_totals_disclose_bounded_report_samples(self):
        self.write("helper.py", "def work(x): return x\n")
        self.write("caller.py", "from helper import work\n" + "work(1)\n" * 40)
        index_workspace(self.root, db_path=self.db)
        symbol = lookup_symbol_details(self.root, "work", db_path=self.db)
        self.assertEqual(symbol["caller_count"], 40)
        self.assertEqual(len(symbol["callers"]), 25)
        self.assertIn("Showing 15 of 40", format_symbol_markdown(symbol))
        impact = analyze_impact(self.root, "work", db_path=self.db)
        self.assertEqual(impact["external_call_count"], 40)
        self.assertIn("Showing 15 of 40", format_impact_markdown(impact))

    def test_partial_and_parse_error_coverage_survives_all_reports(self):
        self.write("a.py", "def work(): return 1\n")
        self.write("b.py", "from a import work\nwork()\n")
        index_workspace(self.root, max_files=1, db_path=self.db)
        reports = (
            (get_repo_map(self.root, db_path=self.db), format_repo_map_markdown),
            (analyze_impact(self.root, "a.py", db_path=self.db), format_impact_markdown),
            (lookup_symbol_details(self.root, "work", db_path=self.db), format_symbol_markdown),
            (get_hotspots_report(self.root, db_path=self.db), format_hotspots_markdown),
        )
        for report, render in reports:
            self.assertTrue(report["scan_status"]["truncated"])
            self.assertIn("Partial index", render(report))
        self.write("b.py", "def broken(:\n")
        index_workspace(self.root, max_files=20, db_path=self.db)
        report = get_repo_map(self.root, db_path=self.db)
        self.assertFalse(report["scan_status"]["truncated"])
        self.assertIn("Incomplete Python analysis", format_repo_map_markdown(report))

    def test_warning_discloses_callers_outside_message_budget(self):
        alert = {"severity": "critical", "symbol_name": "work", "edited_path": "a.py",
                 "old_signature": "work()", "new_signature": "work(x)", "reason": "signature changed",
                 "affected_callers": [{"path": f"caller{i}.py", "line": 2, "caller": "run", "arg_count": 0}
                                      for i in range(40)]}
        message = format_drift_banner([alert])
        self.assertIn("`caller29.py:2`", message)
        self.assertNotIn("`caller30.py:2`", message)
        self.assertIn("10 additional affected caller(s) omitted", message)
        self.assertIn("locus_impact", message)

    def test_persisted_stale_analysis_is_disclosed_in_every_report(self):
        self.write("a.py", "def work(): return 1\n")
        self.write("b.py", "from a import work\nwork()\n")
        index_workspace(self.root, db_path=self.db)
        with open_locus_db(self.db) as conn:
            row = conn.execute("SELECT scan_status_json FROM workspaces WHERE workspace_id = ?",
                               (workspace_id_for(self.root),)).fetchone()
            status = json.loads(row["scan_status_json"])
            status["stale_files"] = [{"path": "b.py", "reason": "file exceeds size limit"}]
            conn.execute("UPDATE workspaces SET scan_status_json = ? WHERE workspace_id = ?",
                         (json.dumps(status), workspace_id_for(self.root)))
        reports = (
            (get_repo_map(self.root, db_path=self.db), format_repo_map_markdown),
            (analyze_impact(self.root, "a.py", db_path=self.db), format_impact_markdown),
            (lookup_symbol_details(self.root, "work", db_path=self.db), format_symbol_markdown),
            (get_hotspots_report(self.root, db_path=self.db), format_hotspots_markdown),
        )
        for report, render in reports:
            message = render(report)
            self.assertIn("Stale index", message)
            self.assertIn("b.py", message)
            self.assertIn("file exceeds size limit", message)
            self.assertIn("warnings remain unresolved", message)

    def test_symbol_excerpt_omits_unavailable_and_oversized_source(self):
        self.write("a.py", "def work(): return 'indexed'\n")
        index_workspace(self.root, db_path=self.db)
        self.assertIn("indexed", lookup_symbol_details(self.root, "work", db_path=self.db)["snippet"])
        for field in ("stale_files", "parse_errors"):
            with self.subTest(field=field):
                with open_locus_db(self.db) as conn:
                    conn.execute("UPDATE workspaces SET scan_status_json = ? WHERE workspace_id = ?",
                                 (json.dumps({field: [{"path": "a.py", "reason": "source unavailable"}]}),
                                  workspace_id_for(self.root)))
                self.assertEqual(lookup_symbol_details(self.root, "work", db_path=self.db)["snippet"], "")
        with open_locus_db(self.db) as conn:
            conn.execute("UPDATE workspaces SET scan_status_json = '{}' WHERE workspace_id = ?",
                         (workspace_id_for(self.root),))
        self.write("a.py", "def work(): return 'changed'\n#" + "x" * (512 * 1024))
        self.assertEqual(lookup_symbol_details(self.root, "work", db_path=self.db)["snippet"], "")


if __name__ == "__main__":
    unittest.main()
