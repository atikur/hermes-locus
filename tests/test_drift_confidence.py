"""A loss of static resolution must not silently prove an existing risk repaired."""

import tempfile
import unittest
from pathlib import Path

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file


class DriftConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"
        self.write("lib.py", "def prepare(old): pass\n")
        self.write("caller.py", "import lib\nlib.prepare(old=1)\n")

    def write(self, path, source):
        (self.root / path).write_text(source)

    def edit(self, path):
        return reindex_edited_file(self.root, path, db_path=self.db)

    def alerts(self):
        return get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]

    def break_contract(self):
        index_workspace(self.root, db_path=self.db)
        self.write("lib.py", "def prepare(new): pass\n")
        self.edit("lib.py")
        self.assertEqual(self.alerts()[0]["severity"], "critical")

    def assert_needs_verification(self):
        alert = self.alerts()[0]
        self.assertEqual(alert["severity"], "warning")
        self.assertIn("need verification", alert["reason"])
        self.assertEqual(alert["affected_callers"][0]["analysis_status"], "needs_verification")
        self.assertNotIn("suggestion", alert["affected_callers"][0])

    def test_decorator_loss_is_warning_until_proven_repair(self):
        self.break_contract()
        self.write("lib.py", "@unknown\ndef prepare(new): pass\n")
        self.edit("lib.py")
        self.assert_needs_verification()
        # Repeated scans and moved call lines preserve the uncertainty.
        self.write("caller.py", "import lib\n\nlib.prepare(old=1)\n")
        self.edit("caller.py")
        index_workspace(self.root, force=True, db_path=self.db)
        self.assert_needs_verification()
        # Restoring a supported target restores the proven critical warning.
        self.write("lib.py", "def prepare(new): pass\n")
        self.edit("lib.py")
        self.assertEqual(self.alerts()[0]["severity"], "critical")
        self.write("caller.py", "import lib\nlib.prepare(new=1)\n")
        self.edit("caller.py")
        self.assertEqual(self.alerts(), [])

    def test_dynamic_target_warning_closes_when_reference_removed(self):
        self.break_contract()
        self.write("lib.py", "exec(source)\ndef prepare(new): pass\n")
        self.edit("lib.py")
        self.assert_needs_verification()
        self.write("caller.py", "import lib\n")
        self.edit("caller.py")
        self.assertEqual(self.alerts(), [])

    def test_ambiguous_reexport_is_unknown_but_changed_import_target_is_removed(self):
        self.write("public.py", "from lib import prepare as exported\n")
        self.write("caller.py", "from public import exported as invoke\ninvoke(old=1)\n")
        self.break_contract()
        self.write("public.py", "if condition:\n    from lib import prepare as exported\n")
        changed = self.edit("public.py")
        self.assertEqual(changed["drift_alerts"][0]["severity"], "warning")
        self.assert_needs_verification()
        self.write("caller.py", "from external import exported as invoke\ninvoke(old=1)\n")
        self.edit("caller.py")
        self.assertEqual(self.alerts(), [])

    def test_unknown_initial_binding_does_not_create_new_warning(self):
        self.write("lib.py", "@unknown\ndef prepare(old): pass\n")
        index_workspace(self.root, db_path=self.db)
        self.write("lib.py", "@unknown\ndef prepare(new): pass\n")
        self.edit("lib.py")
        self.assertEqual(self.alerts(), [])

    def test_caller_confidence_loss_retains_same_expression_as_warning(self):
        cases = (
            ("from lib import prepare\nprepare(old=1)\n",
             "if True:\n    from lib import prepare\nprepare(old=1)\n"),
            ("from lib import prepare\nprepare(old=1)\n",
             "from lib import prepare\nglobals()\nprepare(old=1)\n"),
            ("from lib import prepare as invoke\ninvoke(old=1)\n",
             "if True:\n    from lib import prepare as invoke\ninvoke(old=1)\n"),
            ("import lib\nlib.prepare(old=1)\n",
             "import lib\nglobals()\nlib.prepare(old=1)\n"),
        )
        for before, uncertain in cases:
            with self.subTest(source=uncertain):
                self.write("lib.py", "def prepare(old): pass\n")
                self.write("caller.py", before)
                self.break_contract()
                self.write("caller.py", uncertain)
                result = self.edit("caller.py")
                self.assertEqual(result["drift_alerts"][0]["severity"], "warning")
                self.assert_needs_verification()
                index_workspace(self.root, force=True, db_path=self.db)
                self.assert_needs_verification()
                # A supported, compatible call proves the risk was repaired.
                self.write("caller.py", before.replace("old=", "new="))
                self.edit("caller.py")
                self.assertEqual(self.alerts(), [])

    def test_caller_uncertainty_closes_only_for_removed_expression_or_changed_import(self):
        cases = (
            "from external import prepare\nprepare(old=1)\n",
            "globals()\nother(old=1)\n",
            "pass\n",
        )
        for repaired in cases:
            with self.subTest(source=repaired):
                self.write("lib.py", "def prepare(old): pass\n")
                self.write("caller.py", "from lib import prepare\nprepare(old=1)\n")
                self.break_contract()
                self.write("caller.py", "from lib import prepare\nglobals()\nprepare(old=1)\n")
                self.edit("caller.py")
                self.assert_needs_verification()
                self.write("caller.py", repaired)
                self.edit("caller.py")
                self.assertEqual(self.alerts(), [])

    def test_unknown_initial_caller_does_not_create_a_new_alert(self):
        for caller in (
            "if True:\n    from lib import prepare\nprepare(old=1)\n",
            "from lib import prepare\nglobals()\nprepare(old=1)\n",
        ):
            with self.subTest(source=caller):
                self.write("lib.py", "def prepare(old): pass\n")
                self.write("caller.py", caller)
                index_workspace(self.root, db_path=self.db)
                self.write("lib.py", "def prepare(new): pass\n")
                self.edit("lib.py")
                self.assertEqual(self.alerts(), [])
