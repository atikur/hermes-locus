"""Calls inside the edited module that provably name its own definitions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file
from locus_core.storage import open_locus_db


class SameFileCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"

    def edit(self, path, text):
        (self.root / path).write_text(text, encoding="utf-8")
        return reindex_edited_file(self.root, path, db_path=self.db)["drift_alerts"]

    def start(self, files):
        for path, text in files.items():
            (self.root / path).write_text(text, encoding="utf-8")
        index_workspace(self.root, db_path=self.db)

    def named(self, alerts):
        return {a["symbol_name"]: sorted((c["path"], c["line"], c.get("incompatibility", "")) for c in a["affected_callers"])
                for a in alerts}

    def test_own_functions_classes_and_class_methods_are_checked_in_place(self):
        before = (
            "def helper(value, limit=3):\n    return value\n"            # 1-2
            "class Job:\n"                                               # 3
            "    def __init__(self, name, retries=1):\n        self.name = name\n"   # 4-5
            "    @staticmethod\n    def parse(raw):\n        return raw\n"           # 6-8
            "    def run(self):\n        return helper(self.name, limit=2)\n"       # 9-10
            "def early():\n    return later(1)\n"                        # 11-12
            "def later(value):\n    return helper(value, limit=1)\n"     # 13-14
            "def build():\n    return Job('a', retries=2), Job.parse('x')\n"        # 15-16
            "RESULT = helper(1, limit=5)\n"                              # 17
        )
        self.start({"lib.py": before, "other.py": "from lib import helper\nhelper(1, limit=9)\n"})
        after = (before.replace("def helper(value, limit=3)", "def helper(value, ceiling=3)")
                 .replace("def __init__(self, name, retries=1)", "def __init__(self, name, attempts=1)")
                 .replace("def parse(raw)", "def parse(raw, strict)")
                 .replace("def later(value)", "def later(value, extra)"))
        alerts = self.named(self.edit("lib.py", after))
        keyword = "unexpected keyword argument 'limit'"
        self.assertEqual(alerts, {
            "helper": [("lib.py", 10, keyword), ("lib.py", 14, keyword), ("lib.py", 17, keyword), ("other.py", 2, keyword)],
            "Job": [("lib.py", 16, "unexpected keyword argument 'retries'")],
            "Job.parse": [("lib.py", 16, "missing required argument 'strict'")],
            "later": [("lib.py", 12, "missing required argument 'extra'")],
        })
        repaired = (after.replace("limit=2", "ceiling=2").replace("limit=1", "ceiling=1").replace("limit=5", "ceiling=5")
                    .replace("retries=2", "attempts=2").replace("Job.parse('x')", "Job.parse('x', True)")
                    .replace("later(1)", "later(1, 2)"))
        self.assertEqual(self.named(self.edit("lib.py", repaired)), {"helper": [("other.py", 2, keyword)]})
        self.edit("other.py", "from lib import helper\nhelper(1, ceiling=9)\n")
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_changing_a_definition_and_its_own_callers_together_leaves_no_record(self):
        self.start({"lib.py": "def helper(value, limit=3):\n    return value\n\ndef use():\n    return helper(1, limit=2)\n"})
        alerts = self.edit("lib.py", "def helper(value, ceiling=3):\n    return value\n\ndef use():\n    return helper(1, ceiling=2)\n")
        self.assertEqual(alerts, [])
        with open_locus_db(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM drift_alerts").fetchone()[0], 0)

    def test_names_that_may_not_be_the_module_definition_are_left_alone(self):
        cases = {
            "parameter": "def helper(value):\n    return value\ndef use(helper):\n    return helper(1)\n",
            "local_assignment": "def helper(value):\n    return value\ndef use():\n    helper = print\n    return helper(1)\n",
            "nested_definition": "def helper(value):\n    return value\ndef use():\n    def helper(value):\n        return value\n    return helper(1)\n",
            "redefined": "def helper(value):\n    return value\ndef helper(value):\n    return value\ndef use():\n    return helper(1)\n",
            "conditional": "if flag:\n    def helper(value):\n        return value\ndef use():\n    return helper(1)\n",
            "decorated": "@wrap\ndef helper(value):\n    return value\ndef use():\n    return helper(1)\n",
            "rebound": "def helper(value):\n    return value\nhelper = wrap(helper)\ndef use():\n    return helper(1)\n",
            "global_write": "def helper(value):\n    return value\ndef swap():\n    global helper\n    helper = print\ndef use():\n    return helper(1)\n",
            "dynamic_module": "def helper(value):\n    return value\nglobals()['helper'] = print\ndef use():\n    return helper(1)\n",
            "before_definition": "RESULT = helper(1) if False else None\ndef helper(value):\n    return value\n",
            "instance_receiver": "class Box:\n    def helper(self, value):\n        return value\n    def use(self):\n        return self.helper(1)\n",
            "comprehension_target": "def helper(value):\n    return value\ndef use(items):\n    return [helper(1) for helper in items]\n",
        }
        self.start({f"{name}.py": source for name, source in cases.items()})
        for name, source in cases.items():
            changed = source.replace("def helper(value)", "def helper(value, extra)").replace(
                "def helper(self, value)", "def helper(self, value, extra)")
            self.assertEqual(self.edit(f"{name}.py", changed), [], name)
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])


if __name__ == "__main__":
    unittest.main()


class RepairPatchInAlertTests(unittest.TestCase):
    def test_keyword_rename_alert_carries_one_patch_for_all_mechanical_callers(self):
        from locus_core import format_drift_banner, repair_patch_for_alerts
        with tempfile.TemporaryDirectory() as tmp:
            root, db = Path(tmp) / "repo", Path(tmp) / "index.db"
            root.mkdir()
            files = {
                "lib.py": "def helper(value, limit=3):\n    return value\n\ndef own():\n    return helper(1, limit=2)\n",
                "a.py": "from lib import helper\nhelper(1, limit=9)\nhelper(2)\n",
                "b.py": "import lib\nlib.helper(\n    1,\n    limit=4,\n)\nlib.helper(1, 2, 3)\n",
            }
            for path, text in files.items():
                (root / path).write_text(text, encoding="utf-8")
            index_workspace(root, db_path=db)
            (root / "lib.py").write_text(files["lib.py"].replace("limit=3", "ceiling=3"), encoding="utf-8")
            alerts = reindex_edited_file(root, "lib.py", db_path=db)["drift_alerts"]
            repair = repair_patch_for_alerts(root, alerts, db_path=db)
            self.assertEqual((repair["files"], repair["calls"], repair["skipped"]), (["a.py", "b.py", "lib.py"], 3, []))
            banner = format_drift_banner(alerts, repair)
            self.assertIn("Ready caller patch for the renamed keyword (3 call(s) in 3 file(s))", banner)
            for line in ("+helper(1, ceiling=9)", "+    ceiling=4,", "+    return helper(1, ceiling=2)"):
                self.assertIn(line, banner)
            self.assertNotIn("lib.helper(1, 2, 3)\n+", banner)
            # A change that is not a single rename offers no patch, and the alert text is unchanged.
            (root / "lib.py").write_text(files["lib.py"].replace("limit=3", "limit=3, *, mode"), encoding="utf-8")
            alerts = reindex_edited_file(root, "lib.py", db_path=db)["drift_alerts"]
            self.assertIsNone(repair_patch_for_alerts(root, alerts, db_path=db))
            self.assertEqual(format_drift_banner(alerts, None), format_drift_banner(alerts))
            self.assertNotIn("Ready caller patch", format_drift_banner(alerts))
