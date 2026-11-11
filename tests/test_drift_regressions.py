"""Contract-lifecycle regressions using real source files and the SQLite graph."""

from __future__ import annotations

import sqlite3
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

    def test_same_arity_keyword_rename_tracks_only_newly_broken_calls(self) -> None:
        self.edit(
            "app.py",
            "from lib import calculate\n"
            "keyword = calculate(value=3)\n"
            "positional = calculate(3)\n"
            "already_invalid = calculate(wrong=3)\n",
        )
        self.edit("lib.py", "def calculate(amount, factor=1):\n    return amount * factor\n")
        alert, = self.alerts()
        self.assertEqual(alert["severity"], "critical")
        caller, = alert["affected_callers"]
        self.assertEqual((caller["path"], caller["line"], caller["arg_count"], caller["kind"]),
                         ("app.py", 2, 1, "call"))
        self.assertIn("unexpected keyword argument 'value'", caller["incompatibility"])
        self.assertIn("Unexpected keyword argument 'value'", alert["reason"])
        self.assertEqual(caller["suggestion"], "Review replacing value= with amount=.")

        self.edit("lib.py", "def calculate(total, factor=1):\n    return total * factor + 1\n")
        current, = self.alerts()
        self.assertEqual(current["id"], alert["id"])
        self.assertEqual(current["old_signature"], alert["old_signature"])
        self.assertEqual([c["line"] for c in current["affected_callers"]], [2])
        self.assertEqual(current["affected_callers"][0]["suggestion"], "Review replacing value= with total=.")

        self.edit("app.py", "from lib import calculate\nresult = calculate(total=3)\n")
        self.assertEqual(self.alerts(), [])

    def test_same_arity_keyword_only_change_flags_excess_positionals(self) -> None:
        self.edit(
            "app.py",
            "from lib import calculate\n"
            "broken = calculate(3, 2)\n"
            "compatible = calculate(3, factor=2)\n",
        )
        self.edit("lib.py", "def calculate(value, *, factor=1):\n    return value * factor\n")
        alert, = self.alerts()
        caller, = alert["affected_callers"]
        self.assertEqual((alert["severity"], caller["line"]), ("critical", 2))
        self.assertIn("positional", caller["incompatibility"])

    def test_invalid_attempted_caller_repair_keeps_and_returns_the_warning(self) -> None:
        self.edit("app.py", "from lib import calculate\nresult = calculate(value=3)\n")
        self.edit("lib.py", "def calculate(amount, factor=1):\n    return amount * factor\n")
        previous, = self.alerts()

        result = self.edit("app.py", "from lib import calculate\nresult = calculate(total=3)\n")
        returned, = result["drift_alerts"]
        active, = self.alerts()
        self.assertEqual(active["id"], previous["id"])
        self.assertIn("unexpected keyword argument 'total'", returned["affected_callers"][0]["incompatibility"])

        self.edit("app.py", "from lib import calculate\nresult = calculate(amount=3)\n")
        self.assertEqual(self.alerts(), [])

    def test_same_arity_positional_only_change_flags_explicit_keyword(self) -> None:
        self.edit("app.py", "from lib import calculate\nresult = calculate(value=3)\n")
        self.edit("lib.py", "def calculate(value, /, factor=1):\n    return value * factor\n")
        alert, = self.alerts()
        caller, = alert["affected_callers"]
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("positional-only", caller["incompatibility"])
        self.assertIn("/", alert["new_signature"])
        self.assertNotIn("suggestion", caller)

    def test_unpacked_keyword_rename_warns_until_original_contract_restored(self) -> None:
        self.edit("app.py", "from lib import calculate\nargs = {'value': 3}\nresult = calculate(**args)\n")
        self.edit("lib.py", "def calculate(amount, factor=1):\n    return amount * factor\n")
        alert, = self.alerts()
        self.assertEqual(alert["severity"], "warning")
        self.assertIn("unpacked", alert["reason"])
        self.assertNotIn("incompatibility", alert["affected_callers"][0])

        self.edit("lib.py", "def calculate(value: int, factor=2):\n    return value * factor\n")
        self.assertEqual(self.alerts(), [])

    def test_unpacking_does_not_warn_for_optional_keyword_addition(self) -> None:
        self.edit("app.py", "from lib import calculate\nargs = (3,)\nresult = calculate(*args)\n")
        self.edit("lib.py", "def calculate(value, factor=1, *, offset=0):\n    return value * factor + offset\n")
        self.assertEqual(self.alerts(), [])

    def test_exact_binding_excludes_shadowed_and_attribute_call_targets(self) -> None:
        self.edit(
            "app.py",
            "from lib import calculate\n"
            "def use(calculate, service):\n"
            "    calculate(value=3)\n"
            "    service.calculate(value=4)\n",
        )
        self.edit("lib.py", "def calculate(amount, factor=1):\n    return amount * factor\n")
        self.assertEqual(self.alerts(), [])

    def test_legacy_open_alert_without_shape_still_reconciles_by_arity(self) -> None:
        original = self.break_contract()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE drift_alerts SET baseline_signature_json = NULL")
            conn.execute("UPDATE calls SET python_call_json = NULL")
            conn.execute("UPDATE symbols SET python_signature_json = NULL")
        self.edit("other.py", "def unrelated():\n    return 2\n")
        self.assertEqual(self.alerts()[0]["id"], original["id"])
        self.edit("app.py", "from lib import calculate\nresult = calculate(3, 2)\n")
        self.assertEqual(self.alerts(), [])


if __name__ == "__main__":
    unittest.main()
