"""Impact and hotspot reports use the same bounded test suggestions."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import analyze_impact, get_hotspots_report, index_workspace


class TestTestReachability(unittest.TestCase):
    def test_three_hop_test_is_suggested_consistently_and_four_hops_remain_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            db = Path(tmp) / "locus.db"
            sources = {
                "foundation.py": "def decode():\n    return True\n",
                "tokens.py": "from foundation import decode\ndef verify():\n    return decode()\n",
                "billing.py": "from tokens import verify\ndef charge():\n    return verify()\n",
                "checkout.py": "from billing import charge\ndef checkout():\n    return charge()\n",
                "test_flow.py": "from checkout import checkout\ndef test_purchase():\n    assert checkout()\n",
            }
            for path, source in sources.items():
                (root / path).write_text(source, encoding="utf-8")
            index_workspace(root, db_path=db)

            impact = analyze_impact(root, "tokens.py", db_path=db)
            self.assertEqual(
                [(test["rel_path"], test["depth"]) for test in impact["covering_tests"]],
                [("test_flow.py", 3)],
            )
            # The more distant module must still respect the default three-hop bound.
            distant = analyze_impact(root, "foundation.py", db_path=db)
            self.assertEqual(distant["covering_tests"], [])
            untested = {
                module["rel_path"]
                for module in get_hotspots_report(root, db_path=db)["untested_core"]
            }
            self.assertNotIn("tokens.py", untested)
            self.assertIn("foundation.py", untested)


if __name__ == "__main__":
    unittest.main()
