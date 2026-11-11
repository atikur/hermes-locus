"""Async tests for @sym:, @impact:, @hotspots: ContextReferenceProviders, slash commands & tools."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "locus_ctx_ref_pkg",
        str(REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(REPO_ROOT)]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubCtx:
    def __init__(self, cwd: str) -> None:
        self._cwd = cwd
        self.refs: dict[str, Any] = {}
        self.tools: dict[str, Any] = {}
        self.commands: dict[str, Any] = {}

    def session_cwd(self) -> str:
        return self._cwd

    def get_config(self, key: str, default: Any = None) -> Any:
        return default

    def register_context_reference(self, provider: Any) -> None:
        self.refs[provider.prefix] = provider

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Any, **kwargs: Any) -> None:
        self.tools[name] = handler

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.commands[name] = handler

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **kw: None


class TestContextReferencesAndCommands(unittest.IsolatedAsyncioTestCase):
    async def test_sym_impact_hotspots_autocomplete_and_expand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "app"
            ws.mkdir()
            db_path = Path(tmp) / "refs.db"
            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_path)
            try:
                (ws / "payments.py").write_text(
                    'def capture_payment(order_id: str, amount_cents: int) -> bool:\n    """Capture funds."""\n    return True\n',
                    encoding="utf-8",
                )
                (ws / "checkout.py").write_text(
                    "from payments import capture_payment\ndef finish_order(oid: str) -> bool:\n    return capture_payment(oid, 9900)\n",
                    encoding="utf-8",
                )

                mod = _load_plugin_module()
                ctx = _StubCtx(str(ws))
                mod.register(ctx)

                self.assertEqual(set(ctx.refs.keys()), {"sym", "impact", "hotspots"})

                # @sym: autocomplete + expand
                sym_items = await ctx.refs["sym"].autocomplete("capture")
                self.assertTrue(any(i.text == "capture_payment" for i in sym_items))
                sym_expanded = await ctx.refs["sym"].expand("capture_payment")
                self.assertIsNotNone(sym_expanded)
                assert sym_expanded is not None
                self.assertIn("payments.py", sym_expanded)
                self.assertIn("checkout.py:3", sym_expanded)

                # @impact: autocomplete + expand
                imp_items = await ctx.refs["impact"].autocomplete("payments")
                self.assertTrue(any("payments.py" in i.text for i in imp_items))
                imp_expanded = await ctx.refs["impact"].expand("payments.py")
                self.assertIsNotNone(imp_expanded)
                assert imp_expanded is not None
                self.assertIn("Blast-Radius Report", imp_expanded)
                self.assertIn("checkout.py", imp_expanded)

                # @hotspots: autocomplete + expand
                hot_items = await ctx.refs["hotspots"].autocomplete("")
                self.assertGreaterEqual(len(hot_items), 3)
                hot_expanded = await ctx.refs["hotspots"].expand("all")
                self.assertIsNotNone(hot_expanded)
                assert hot_expanded is not None
                self.assertIn("Architectural Hotspots", hot_expanded)

                # Verify all 4 model tools return valid JSON envelopes
                for tname, targs in (
                    ("locus_map", {}),
                    ("locus_symbol", {"name": "capture_payment"}),
                    ("locus_impact", {"target": "payments.py"}),
                    ("locus_hotspots", {}),
                ):
                    out = json.loads(ctx.tools[tname](targs))
                    self.assertTrue(out["ok"], f"{tname} failed: {out}")

                # Verify /locus slash command subcommands
                slash = ctx.commands["locus"]
                self.assertIn("Locus Graph Status", slash("status"))
                self.assertIn("Architectural Map", slash("map"))
                self.assertIn("Blast-Radius Report", slash("impact payments.py"))
                self.assertIn("capture_payment", slash("symbol capture_payment"))
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env


if __name__ == "__main__":
    unittest.main()
