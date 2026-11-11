"""Contract-lifecycle regressions using real source files and the SQLite graph."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file


class TestDriftRegressions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "locus.db"
        self.write("lib.py", "def calculate(value, factor=1):\n    return value * factor\n")
        self.write("app.py", "from lib import calculate\nresult = calculate(3)\n")
        self.write("other.py", "def unrelated():\n    return 1\n")
        index_workspace(self.root, db_path=self.db)

    def write(self, path: str, source: str) -> None:
        (self.root / path).write_text(source, encoding="utf-8")

    def edit(self, path: str, source: str) -> dict:
        self.write(path, source)
        return reindex_edited_file(self.root, path, db_path=self.db)

    def alerts(self) -> list[dict]:
        return get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]

    def break_contract(self) -> dict:
        result = self.edit("lib.py", "def calculate(value, factor):\n    return value * factor\n")
        self.assertEqual(len(result["drift_alerts"]), 1)
        return self.alerts()[0]

    def test_alert_survives_body_only_and_unrelated_edits_without_duplicates(self) -> None:
        original = self.break_contract()
        result = self.edit("lib.py", "def calculate(value, factor):\n    return value * factor + 1\n")
        self.assertEqual(len(result["drift_alerts"]), 1)
        self.edit("other.py", "def unrelated():\n    return 2\n")
        active = self.alerts()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], original["id"])
        self.assertEqual(active[0]["old_signature"], original["old_signature"])
        self.assertEqual(active[0]["affected_callers"][0]["path"], "app.py")

    def test_partial_caller_fix_refreshes_remaining_sites_then_resolves(self) -> None:
        self.edit("second.py", "from lib import calculate\nresult = calculate(4)\n")
        original = self.break_contract()
        self.assertEqual(len(original["affected_callers"]), 2)
        self.edit("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        remaining = self.alerts()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["id"], original["id"])
        self.assertEqual([c["path"] for c in remaining[0]["affected_callers"]], ["second.py"])
        self.edit("second.py", "from lib import calculate\nresult = calculate(4, 2)\n")
        self.assertEqual(self.alerts(), [])

    def test_restoring_optional_parameter_resolves_after_repeated_callee_edits(self) -> None:
        original = self.break_contract()
        self.edit("lib.py", "def calculate(value, factor, offset):\n    return value * factor + offset\n")
        self.assertEqual(self.alerts()[0]["id"], original["id"])
        self.assertEqual(self.alerts()[0]["old_signature"], original["old_signature"])
        restored = self.edit("lib.py", "def calculate(value, factor=1):\n    return value * factor\n")
        self.assertEqual(restored["drift_alerts"], [])
        self.assertEqual(self.alerts(), [])

    def test_deleted_symbol_alert_survives_edits_until_import_and_call_removed(self) -> None:
        deleted = self.edit("lib.py", "def replacement():\n    return 1\n")
        self.assertEqual(deleted["drift_alerts"][0]["new_signature"], "<removed>")
        original_id = self.alerts()[0]["id"]
        self.edit("lib.py", "def replacement():\n    return 2\n")
        self.assertEqual(self.alerts()[0]["id"], original_id)
        # A leftover import is still broken even after the invocation is gone.
        self.edit("app.py", "from lib import calculate\nresult = 3\n")
        self.assertEqual(self.alerts()[0]["affected_callers"][0]["kind"], "import")
        self.edit("app.py", "result = 3\n")
        self.assertEqual(self.alerts(), [])

    def test_deleted_file_alert_survives_repeat_hook_and_resolves_on_restore(self) -> None:
        (self.root / "lib.py").unlink()
        first = reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(len(first["drift_alerts"]), 1)
        original_id = self.alerts()[0]["id"]
        repeated = reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(len(repeated["drift_alerts"]), 1)
        self.assertEqual(self.alerts()[0]["id"], original_id)
        self.edit("lib.py", "def calculate(value, factor=1):\n    return value * factor\n")
        self.assertEqual(self.alerts(), [])

    def test_emptying_a_file_detects_removed_exports(self) -> None:
        emptied = self.edit("lib.py", "")
        self.assertEqual(len(emptied["drift_alerts"]), 1)
        self.assertEqual(emptied["drift_alerts"][0]["new_signature"], "<removed>")

    def test_compatible_calls_and_import_only_users_do_not_raise_arity_warning(self) -> None:
        self.edit("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        self.edit("importer.py", "from lib import calculate\n")
        result = self.edit("lib.py", "def calculate(value, factor):\n    return value * factor\n")
        self.assertEqual(result["drift_alerts"], [])
        self.assertEqual(self.alerts(), [])

    def test_unknown_arity_warning_survives_until_contract_or_caller_is_known_safe(self) -> None:
        self.edit("app.py", "from lib import calculate\nargs = (3,)\nresult = calculate(*args)\n")
        original = self.break_contract()
        self.assertEqual(original["severity"], "warning")
        self.edit("other.py", "def unrelated():\n    return 2\n")
        self.edit("lib.py", "def calculate(value, factor):\n    return factor * value\n")
        self.assertEqual(self.alerts()[0]["severity"], "warning")
        self.edit("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        self.assertEqual(self.alerts(), [])

    def test_unknown_call_can_resolve_by_restoring_original_signature(self) -> None:
        self.edit("app.py", "from lib import calculate\nargs = (3,)\nresult = calculate(*args)\n")
        self.break_contract()
        self.edit("lib.py", "def calculate(value, factor=1):\n    return value * factor\n")
        self.assertEqual(self.alerts(), [])

    def test_scan_reconciles_alert_after_caller_fixed_outside_hook(self) -> None:
        self.break_contract()
        self.write("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        index_workspace(self.root, force=True, db_path=self.db)
        self.assertEqual(self.alerts(), [])

    def test_broken_caller_beyond_display_limit_is_still_detected(self) -> None:
        for i in range(26):
            self.write(f"a{i:02}.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        self.write("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        self.write("z_broken.py", "from lib import calculate\nresult = calculate(3)\n")
        index_workspace(self.root, force=True, db_path=self.db)
        alert = self.break_contract()
        self.assertEqual([c["path"] for c in alert["affected_callers"]], ["z_broken.py"])


if __name__ == "__main__":
    unittest.main()
