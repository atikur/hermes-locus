"""Unit tests for multi-language parsers, incremental indexing, PageRank, cycles & blast radius."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from locus_core import (  # noqa: E402
    analyze_impact,
    build_visual_graph,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
    lookup_symbol_details,
    parse_source_file,
    search_symbols,
)


class TestParsersAndGraph(unittest.TestCase):
    def test_python_parser_extracts_arity_methods_and_calls(self) -> None:
        src = '''
"""Auth module."""
from .crypto import hash_secret

class TokenVerifier:
    """Verifies JWTs."""
    def verify(self, token: str, strict: bool = True) -> bool:
        digest = hash_secret(token)
        return bool(digest)

async def issue_token(user_id: str, *scopes: str) -> str:
    return hash_secret(user_id)
'''
        parsed = parse_source_file("auth/tokens.py", src)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        by_qname = {s.qualified_name: s for s in parsed.symbols}
        self.assertIn("TokenVerifier", by_qname)
        self.assertIn("TokenVerifier.verify", by_qname)
        self.assertIn("issue_token", by_qname)

        verify_sym = by_qname["TokenVerifier.verify"]
        self.assertEqual(verify_sym.min_args, 1)
        self.assertEqual(verify_sym.max_args, 2)

        issue_sym = by_qname["issue_token"]
        self.assertEqual(issue_sym.min_args, 1)
        self.assertEqual(issue_sym.max_args, -1)

        self.assertEqual(len(parsed.imports), 1)
        self.assertEqual(parsed.imports[0].target_spec, ".crypto")
        callee_names = {c.callee_name for c in parsed.calls}
        self.assertIn("hash_secret", callee_names)

    def test_multilanguage_parsers_ts_go_rust(self) -> None:
        ts_src = """
import { formatPrice } from './currency.js';
export async function checkoutCart(cartId: string, coupon?: string) {
    return formatPrice(100);
}
export interface CartReceipt { total: number; }
"""
        ts_parsed = parse_source_file("src/checkout.ts", ts_src)
        self.assertIsNotNone(ts_parsed)
        assert ts_parsed is not None
        ts_names = {s.name for s in ts_parsed.symbols}
        self.assertIn("checkoutCart", ts_names)
        self.assertIn("CartReceipt", ts_names)
        self.assertEqual(ts_parsed.imports[0].target_spec, "./currency.js")

        go_src = """
package billing
import "github.com/acme/app/ledger"
func ChargeCustomer(customerID string, cents int64) error {
    return ledger.Record(customerID)
}
"""
        go_parsed = parse_source_file("billing/charge.go", go_src)
        self.assertIsNotNone(go_parsed)
        assert go_parsed is not None
        self.assertEqual(go_parsed.symbols[0].name, "ChargeCustomer")
        self.assertEqual(go_parsed.symbols[0].min_args, 2)

        rs_src = """
use crate::storage:: open_pool;
pub struct LedgerEngine {}
pub async fn settle_invoice(invoice_id: u64) -> bool {
    open_pool();
    true
}
"""
        rs_parsed = parse_source_file("src/billing.rs", rs_src)
        self.assertIsNotNone(rs_parsed)
        assert rs_parsed is not None
        rs_names = {s.name for s in rs_parsed.symbols}
        self.assertIn("LedgerEngine", rs_names)
        self.assertIn("settle_invoice", rs_names)

    def test_incremental_indexing_pagerank_blast_radius_and_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "project"
            db_file = Path(tmp) / "locus_test.db"
            (ws / "core").mkdir(parents=True)
            (ws / "services").mkdir(parents=True)
            (ws / "api").mkdir(parents=True)
            (ws / "tests").mkdir(parents=True)
            (ws / ".secret_dir").mkdir(parents=True)
            (ws / "ignored_dir").mkdir(parents=True)

            (ws / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
            (ws / ".secret_dir" / "keys.py").write_text("SECRET = 1\n", encoding="utf-8")
            (ws / "ignored_dir" / "skip.py").write_text("SKIP = 1\n", encoding="utf-8")

            # Core hub module imported by services
            (ws / "core" / "db.py").write_text(
                "def query_row(sql: str, params: tuple = ()) -> dict:\n    return {}\n",
                encoding="utf-8",
            )
            (ws / "services" / "users.py").write_text(
                "from core.db import query_row\n\ndef get_user(uid: str) -> dict:\n    return query_row('SELECT 1', (uid,))\n",
                encoding="utf-8",
            )
            (ws / "api" / "routes.py").write_text(
                "from services.users import get_user\n\ndef handle_profile(uid: str) -> dict:\n    return get_user(uid)\n",
                encoding="utf-8",
            )
            (ws / "tests" / "test_routes.py").write_text(
                "from api.routes import handle_profile\n\ndef test_profile():\n    assert handle_profile('u1') == {}\n",
                encoding="utf-8",
            )

            # First scan
            res1 = index_workspace(ws, db_path=db_file)
            self.assertTrue(res1["ok"])
            self.assertEqual(res1["file_count"], 4)
            self.assertEqual(res1["updated_files"], 4)

            # Second incremental scan with zero changes -> 0 updated_files
            res2 = index_workspace(ws, db_path=db_file)
            self.assertEqual(res2["updated_files"], 0)

            # Repo map ranks core/db.py at the top by PageRank
            rmap = get_repo_map(ws, db_path=db_file)
            self.assertEqual(rmap["modules"][0]["rel_path"], "core/db.py")
            self.assertEqual(rmap["cycles"], [])

            # Transitive blast radius on core/db.py:
            # Hop 1 -> services/users.py
            # Hop 2 -> api/routes.py
            # Hop 3 (test) -> tests/test_routes.py
            impact = analyze_impact(ws, "core/db.py", max_depth=3, db_path=db_file)
            self.assertTrue(impact["ok"])
            self.assertEqual([d["rel_path"] for d in impact["direct_dependents"]], ["services/users.py"])
            self.assertEqual([d["rel_path"] for d in impact["transitive_dependents"]], ["api/routes.py"])
            self.assertEqual([t["rel_path"] for t in impact["covering_tests"]], ["tests/test_routes.py"])

            # Visual graph layout includes blast roles
            vgraph = build_visual_graph(ws, focus_target="core/db.py", db_path=db_file)
            roles = {n["id"]: n["role"] for n in vgraph["nodes"]}
            self.assertEqual(roles["core/db.py"], "focus")
            self.assertEqual(roles["services/users.py"], "direct_dependent")
            self.assertEqual(roles["api/routes.py"], "transitive_dependent")
            self.assertEqual(roles["tests/test_routes.py"], "covering_test")

            # Symbol search & neighborhood details
            syms = search_symbols(ws, "query_row", db_path=db_file)
            self.assertEqual(syms[0]["name"], "query_row")
            detail = lookup_symbol_details(ws, "query_row", db_path=db_file)
            self.assertTrue(detail["ok"])
            self.assertEqual(detail["callers"][0]["path"], "services/users.py")

            # Introduce a circular import between cyc_a.py and cyc_b.py
            (ws / "cyc_a.py").write_text("import cyc_b\ndef fa(): return 1\n", encoding="utf-8")
            (ws / "cyc_b.py").write_text("import cyc_a\ndef fb(): return 2\n", encoding="utf-8")
            index_workspace(ws, force=True, db_path=db_file)
            hotspots = get_hotspots_report(ws, db_path=db_file)
            self.assertEqual(len(hotspots["cycles"]), 1)
            self.assertIn("cyc_a.py", hotspots["cycles"][0])
            self.assertIn("cyc_b.py", hotspots["cycles"][0])

    def test_symlink_escape_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            secret = outside / "outside_mod.py"
            secret.write_text(" def leaked(): pass\n", encoding="utf-8")

            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "main.py").write_text("def ok(): return 1\n", encoding="utf-8")
            os.symlink(secret, ws / "escaped.py")

            db_file = Path(tmp) / "sym.db"
            res = index_workspace(ws, db_path=db_file)
            self.assertEqual(res["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
