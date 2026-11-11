"""Comprehensive edge-case, CLI, FastAPI drift-resolution, and Hermes @-reference integration tests."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.plugin_api import router as locus_router  # noqa: E402
from locus_core import (  # noqa: E402
    analyze_impact,
    build_visual_graph,
    get_hotspots_report,
    get_repo_map,
    index_workspace,
    lookup_symbol_details,
    merge_drift_into_tool_result,
    reindex_edited_file,
    validate_safe_workspace_root,
)


def _load_plugin_root():
    spec = importlib.util.spec_from_file_location(
        "locus_edge_test_pkg",
        str(REPO_ROOT / "__init__.py"),
        submodule_search_locations=[str(REPO_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(REPO_ROOT)]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestEdgeCasesAndCLI(unittest.IsolatedAsyncioTestCase):
    def test_empty_workspace_behaves_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "empty_ws"
            ws.mkdir()
            db_file = Path(tmp) / "empty.db"

            rmap = get_repo_map(ws, db_path=db_file)
            self.assertTrue(rmap["ok"])
            self.assertEqual(rmap["file_count"], 0)
            self.assertEqual(rmap["modules"], [])

            imp = analyze_impact(ws, "nonexistent.py", db_path=db_file)
            self.assertFalse(imp["ok"])

            sym = lookup_symbol_details(ws, "missing_fn", db_path=db_file)
            self.assertFalse(sym["ok"])

            hot = get_hotspots_report(ws, db_path=db_file)
            self.assertTrue(hot["ok"])
            self.assertEqual(hot["cycles"], [])

            vgraph = build_visual_graph(ws, db_path=db_file)
            self.assertTrue(vgraph["ok"])
            self.assertEqual(vgraph["nodes"], [])

    def test_malformed_python_and_binary_files_fail_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "broken_ws"
            ws.mkdir()
            db_file = Path(tmp) / "broken.db"

            (ws / "valid.py").write_text("def healthy(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
            (ws / "syntax_broken.py").write_text("def broken(:\n    return ???\n", encoding="utf-8")
            (ws / "blob.py").write_bytes(b"\x00\xff\xfe\x00binary_payload")

            res = index_workspace(ws, db_path=db_file)
            self.assertTrue(res["ok"])
            # valid.py indexes cleanly; broken syntax & binary NUL file never crash indexing
            rmap = get_repo_map(ws, db_path=db_file)
            paths = [m["rel_path"] for m in rmap["modules"]]
            self.assertIn("valid.py", paths)
            self.assertNotIn("blob.py", paths)

            # Editing broken file also fails open gracefully
            re_out = reindex_edited_file(ws, "syntax_broken.py", db_path=db_file)
            self.assertTrue(re_out["ok"])

    def test_typescript_js_extension_resolution_and_three_node_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ts_ws"
            (ws / "src").mkdir(parents=True)
            db_file = Path(tmp) / "ts.db"

            (ws / "src" / "money.ts").write_text(
                "export function formatCents(cents: number): string { return String(cents); }\n",
                encoding="utf-8",
            )
            (ws / "src" / "cart.ts").write_text(
                "import { formatCents } from './money.js';\n"
                "export function renderTotal(c: number): string { return formatCents(c); }\n",
                encoding="utf-8",
            )
            # 3-node cycle: a -> b -> c -> a
            (ws / "src" / "a.ts").write_text("import './b.js';\nexport const A = 1;\n", encoding="utf-8")
            (ws / "src" / "b.ts").write_text("import './c.js';\nexport const B = 2;\n", encoding="utf-8")
            (ws / "src" / "c.ts").write_text("import './a.js';\nexport const C = 3;\n", encoding="utf-8")

            index_workspace(ws, db_path=db_file)

            imp = analyze_impact(ws, "src/money.ts", db_path=db_file)
            self.assertTrue(imp["ok"])
            self.assertEqual([d["rel_path"] for d in imp["direct_dependents"]], ["src/cart.ts"])

            hot = get_hotspots_report(ws, db_path=db_file)
            self.assertEqual(len(hot["cycles"]), 1)
            self.assertEqual(set(hot["cycles"][0]), {"src/a.ts", "src/b.ts", "src/c.ts"})

    def test_merge_drift_into_tool_result_formats(self) -> None:
        banner = "[Locus Signature Drift Alert]\n- Broken caller at app.py:10"

        # 1. JSON dict with 'output'
        j_out = merge_drift_into_tool_result(json.dumps({"status": "ok", "output": "Wrote file"}), banner)
        d_out = json.loads(j_out)
        self.assertIn(banner, d_out["output"])
        self.assertEqual(d_out["locus_drift_alert"], banner)

        # 2. JSON dict without 'output' or 'message'
        j_custom = merge_drift_into_tool_result(json.dumps({"written_bytes": 128}), banner)
        d_custom = json.loads(j_custom)
        self.assertEqual(d_custom["written_bytes"], 128)
        self.assertEqual(d_custom["locus_drift_alert"], banner)

        # 3. Plain text tool output
        plain = merge_drift_into_tool_result("Patch applied cleanly.", banner)
        self.assertTrue(plain.startswith("Patch applied cleanly."))
        self.assertIn(banner, plain)

    def test_cli_subcommands_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "cli_ws"
            ws.mkdir()
            db_file = Path(tmp) / "cli.db"
            (ws / "core.py").write_text("def ping() -> str:\n    return 'pong'\n", encoding="utf-8")
            (ws / "api.py").write_text("from core import ping\ndef handler() -> str:\n    return ping()\n", encoding="utf-8")

            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_file)
            try:
                mod = _load_plugin_root()
                parser = argparse.ArgumentParser(prog="hermes locus")
                mod._setup_cli_parser(parser)

                for argv in (
                    ["scan", "-w", str(ws), "--json"],
                    ["map", "-w", str(ws), "--json"],
                    ["impact", "core.py", "-w", str(ws), "--json"],
                    ["symbol", "ping", "-w", str(ws), "--json"],
                    ["hotspots", "-w", str(ws), "--json"],
                ):
                    parsed = parser.parse_args(argv)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = mod._handle_cli_command(parsed)
                    self.assertEqual(rc, 0, f"CLI failed for {argv}: {buf.getvalue()}")
                    payload = json.loads(buf.getvalue())
                    self.assertTrue(payload["ok"])

                # Missing target returns exit code 2
                buf_err = io.StringIO()
                with redirect_stdout(buf_err):
                    rc_err = mod._handle_cli_command(parser.parse_args(["impact", "-w", str(ws)]))
                self.assertEqual(rc_err, 2)
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env

    def test_manual_drift_resolution_via_fastapi(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "api_drift_ws"
            ws.mkdir()
            db_file = Path(tmp) / "api_drift.db"
            (ws / "svc.py").write_text("def send(msg: str) -> bool:\n    return True\n", encoding="utf-8")
            (ws / "caller.py").write_text("from svc import send\ndef notify():\n    return send('hi')\n", encoding="utf-8")

            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_file)
            try:
                index_workspace(ws, db_path=db_file)
                (ws / "svc.py").write_text("def send(msg: str, channel: str) -> bool:\n    return True\n", encoding="utf-8")
                reindex_edited_file(ws, "svc.py", db_path=db_file)

                app = FastAPI()
                app.include_router(locus_router, prefix="/api/plugins/locus-codegraph")
                client = TestClient(app)

                hot1 = client.get("/api/plugins/locus-codegraph/hotspots", params={"workspace": str(ws)}).json()
                self.assertEqual(len(hot1["open_drift_alerts"]), 1)
                alert_id = hot1["open_drift_alerts"][0]["id"]

                res = client.post("/api/plugins/locus-codegraph/resolve-drift", json={"alert_id": alert_id})
                self.assertEqual(res.status_code, 200)
                self.assertTrue(res.json()["ok"])
                self.assertEqual(res.json()["resolved_id"], alert_id)

                hot2 = client.get("/api/plugins/locus-codegraph/hotspots", params={"workspace": str(ws)}).json()
                self.assertEqual(len(hot2["open_drift_alerts"]), 0)
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env

    def test_failed_tool_call_does_not_trigger_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "err_ws"
            ws.mkdir()
            db_file = Path(tmp) / "err.db"
            (ws / "a.py").write_text("def fn(x: int) -> int:\n    return x\n", encoding="utf-8")
            (ws / "b.py").write_text("from a import fn\ndef run():\n    return fn(1)\n", encoding="utf-8")

            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_file)
            try:
                mod = _load_plugin_root()

                class _Ctx:
                    def session_cwd(self) -> str:
                        return str(ws)

                    def get_config(self, k: str, d: Any = None) -> Any:
                        return d

                    def register_hook(self, name: str, cb: Any, **kw: Any) -> None:
                        self.hooks[name] = cb

                    def __init__(self) -> None:
                        self.hooks: dict[str, Any] = {}

                    def __getattr__(self, name: str) -> Any:
                        return lambda *a, **kw: None

                ctx = _Ctx()
                mod.register(ctx)
                ctx.hooks["on_session_start"]()

                # Write breaking signature on disk, BEFORE simulating an errored tool result
                (ws / "a.py").write_text("def fn(x: int, y: int) -> int:\n    return x + y\n", encoding="utf-8")
                err_payload = json.dumps({"error": "Patch conflict"})
                ctx.hooks["post_tool_call"](
                    tool_name="patch",
                    args={"path": "a.py"},
                    result=err_payload,
                    tool_call_id="err_1",
                )
                transformed = ctx.hooks["transform_tool_result"](
                    tool_name="patch",
                    args={"path": "a.py"},
                    result=err_payload,
                    tool_call_id="err_1",
                )
                self.assertIsNone(transformed)
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env

    async def test_hermes_context_reference_preprocessor_end_to_end(self) -> None:
        try:
            from agent.context_references import (
                preprocess_context_references_async,
                register_context_reference_provider,
            )
        except ImportError:
            self.skipTest("agent.context_references not on PYTHONPATH")

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "e2e_refs"
            ws.mkdir()
            db_file = Path(tmp) / "e2e_refs.db"
            (ws / "ledger.py").write_text(
                "def book_entry(account: str, cents: int) -> str:\n    return account\n",
                encoding="utf-8",
            )
            (ws / "invoicing.py").write_text(
                "from ledger import book_entry\ndef issue_invoice(acc: str) -> str:\n    return book_entry(acc, 500)\n",
                encoding="utf-8",
            )

            old_env = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(db_file)
            try:
                mod = _load_plugin_root()

                class _RefCtx:
                    def session_cwd(self) -> str:
                        return str(ws)

                    def get_config(self, k: str, d: Any = None) -> Any:
                        return d

                rctx = _RefCtx()
                for prov in (
                    mod.LocusSymbolReferenceProvider(rctx),
                    mod.LocusImpactReferenceProvider(rctx),
                    mod.LocusHotspotsReferenceProvider(rctx),
                ):
                    pub = register_context_reference_provider(prov, owner="locus_test", reclaim=True)
                    if callable(pub):
                        pub()

                res = await preprocess_context_references_async(
                    "Please refactor @sym:book_entry safely using @impact:ledger.py and @hotspots:cycles",
                    cwd=ws,
                    context_length=32000,
                )
                self.assertTrue(res.expanded)
                self.assertEqual(res.warnings, [])
                self.assertIn("book_entry", res.message)
                self.assertIn("invoicing.py", res.message)
                self.assertIn("Blast-Radius Report", res.message)
            finally:
                if old_env is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = old_env

    def test_safe_workspace_root_blocks_credentials_system_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake_home"
            ssh_keys = fake_home / ".ssh" / "keys"
            hermes_prof = fake_home / ".hermes" / "profiles" / "default"
            normal_proj = fake_home / "projects" / "demo_app"
            hidden_leaf = fake_home / "projects" / ".secret_dir"
            sym_root = Path(tmp) / "sym_ws"

            ssh_keys.mkdir(parents=True)
            hermes_prof.mkdir(parents=True)
            normal_proj.mkdir(parents=True)
            hidden_leaf.mkdir(parents=True)
            sym_root.symlink_to(normal_proj)
            (normal_proj / "app.py").write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(fake_home)
            try:
                self.assertFalse(validate_safe_workspace_root("/")[0])
                self.assertFalse(validate_safe_workspace_root("/etc")[0])
                self.assertFalse(validate_safe_workspace_root(fake_home)[0])
                self.assertFalse(validate_safe_workspace_root(ssh_keys)[0])
                self.assertFalse(validate_safe_workspace_root(hermes_prof)[0])
                self.assertFalse(validate_safe_workspace_root(hidden_leaf)[0])
                self.assertFalse(validate_safe_workspace_root(sym_root)[0])
                self.assertTrue(validate_safe_workspace_root(normal_proj)[0])

                bad_idx = index_workspace(ssh_keys, db_path=Path(tmp) / "sec.db")
                self.assertFalse(bad_idx["ok"])
                self.assertFalse(get_repo_map(ssh_keys, db_path=Path(tmp) / "sec.db")["ok"])

                app = FastAPI()
                app.include_router(locus_router, prefix="/api/plugins/locus-codegraph")
                client = TestClient(app)
                for blocked in (fake_home, ssh_keys, hermes_prof, hidden_leaf, sym_root):
                    resp = client.get("/api/plugins/locus-codegraph/summary", params={"workspace": str(blocked)})
                    self.assertEqual(resp.status_code, 403, f"Expected 403 for {blocked}, got {resp.status_code}")
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

    def test_deleted_symbol_and_default_restored_drift_auto_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "autoresolve_ws"
            ws.mkdir()
            db_file = Path(tmp) / "autoresolve.db"

            # Part A: Deleted symbol drift auto-resolves when caller removes import & call
            (ws / "auth.py").write_text(
                "def legacy_check(tok: str) -> bool:\n    return True\n\ndef current_check(tok: str) -> bool:\n    return True\n",
                encoding="utf-8",
            )
            (ws / "routes.py").write_text(
                "from auth import legacy_check\ndef handle(t: str) -> bool:\n    return legacy_check(t)\n",
                encoding="utf-8",
            )
            index_workspace(ws, db_path=db_file)

            (ws / "auth.py").write_text(
                "def current_check(tok: str) -> bool:\n    return True\n",
                encoding="utf-8",
            )
            r_del = reindex_edited_file(ws, "auth.py", db_path=db_file)
            self.assertEqual(len(r_del["drift_alerts"]), 1)
            self.assertEqual(get_repo_map(ws, db_path=db_file)["open_drift_count"], 1)

            (ws / "routes.py").write_text(
                "from auth import current_check\ndef handle(t: str) -> bool:\n    return current_check(t)\n",
                encoding="utf-8",
            )
            reindex_edited_file(ws, "routes.py", db_path=db_file)
            self.assertEqual(get_repo_map(ws, db_path=db_file)["open_drift_count"], 0)

            # Part B: Arity drift auto-resolves if author makes new parameter optional in defining file
            (ws / "auth.py").write_text(
                "def current_check(tok: str, strict: bool) -> bool:\n    return strict\n",
                encoding="utf-8",
            )
            r_arity = reindex_edited_file(ws, "auth.py", db_path=db_file)
            self.assertEqual(len(r_arity["drift_alerts"]), 1)
            self.assertEqual(get_repo_map(ws, db_path=db_file)["open_drift_count"], 1)

            (ws / "auth.py").write_text(
                "def current_check(tok: str, strict: bool = False) -> bool:\n    return strict\n",
                encoding="utf-8",
            )
            reindex_edited_file(ws, "auth.py", db_path=db_file)
            self.assertEqual(get_repo_map(ws, db_path=db_file)["open_drift_count"], 0)

    def test_reindex_edited_file_skips_symlinks_oversized_and_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "guards_ws"
            ws.mkdir()
            db_file = Path(tmp) / "guards.db"
            (ws / "good.py").write_text("def a() -> int:\n    return 1\n", encoding="utf-8")
            index_workspace(ws, db_path=db_file)

            (ws / "link.py").symlink_to(ws / "good.py")
            res_sym = reindex_edited_file(ws, "link.py", db_path=db_file)
            self.assertTrue(res_sym["skipped"])
            self.assertEqual(res_sym["reason"], "symlink")

            (ws / "bin_mod.py").write_bytes(b"def x():\x00\x01\x02\n")
            res_bin = reindex_edited_file(ws, "bin_mod.py", db_path=db_file)
            self.assertTrue(res_bin["skipped"])
            self.assertEqual(res_bin["reason"], "binary_file")

            (ws / "huge.py").write_text("x = 1\n" * 100_000, encoding="utf-8")
            res_huge = reindex_edited_file(ws, "huge.py", db_path=db_file)
            self.assertTrue(res_huge["skipped"])
            self.assertEqual(res_huge["reason"], "size_out_of_bounds")


if __name__ == "__main__":
    unittest.main()
