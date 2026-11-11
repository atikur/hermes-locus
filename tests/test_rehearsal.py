"""Signature rehearsal: predict caller breakage before any edit, without writing anything."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from locus_core import (
    format_rehearsal_markdown, get_hotspots_report, index_workspace, rehearse_signature_change,
    reindex_edited_file,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

TOKENS = "def verify_token(token, audience=None):\n    return token\n\ndef local():\n    return verify_token('t')\n"
FILES = {
    "auth/__init__.py": "",
    "auth/tokens.py": TOKENS,
    "checkout.py": "from auth.tokens import verify_token\n\ndef charge(token):\n    return verify_token(\n        token,\n        audience='checkout',\n    )\n",
    "api.py": "import auth.tokens as tokens\nresult = tokens.verify_token('t', audience='api')  # café\nplain = tokens.verify_token('t')\nmany = tokens.verify_token(*args, **options)\n",
    "models.py": ("import dataclasses\n"
                  "class Client:\n    def __init__(self, host, timeout=5):\n        self.host = host\n"
                  "    @classmethod\n    def build(cls, raw):\n        return cls(raw)\n"
                  "    def send(self, body):\n        return body\n"
                  "@dataclasses.dataclass\nclass Record:\n    host: str\n"),
    "use_models.py": "from models import Client, Record\nClient('h', timeout=1)\nClient.build('x')\nRecord('h')\n",
    "other.py": "def verify_token(token):\n    return token\n",
}


class RehearsalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.db = Path(self.tmp.name) / "index.db"
        for path, text in FILES.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        index_workspace(self.root, db_path=self.db)

    def rehearse(self, target, params):
        return rehearse_signature_change(self.root, target, params, db_path=self.db)

    def sites(self, rows):
        return sorted(f"{row['path']}:{row['line']}" for row in rows)

    def test_predicts_breaks_reasons_and_writes_nothing(self):
        before = {path: (self.root / path).read_bytes() for path in FILES}
        data = self.rehearse("auth/tokens.py::verify_token", "(token, scope=None)")
        self.assertTrue(data["ok"])
        self.assertEqual(self.sites(data["breaks"]), ["api.py:2", "checkout.py:4"])
        self.assertTrue(all(row["incompatibility"] == "unexpected keyword argument 'audience'" for row in data["breaks"]))
        self.assertTrue(all(row["suggestion"] == "Replace audience= with scope=." for row in data["breaks"]))
        self.assertEqual(self.sites(data["needs_review"]), ["api.py:4"])
        # The module's own call to verify_token is checked too, and stays valid here.
        self.assertEqual((data["checked_call_sites"], data["compatible_count"]), (5, 2))
        self.assertEqual(data["unchecked"], {"same_file": 0, "unresolved_same_name": 0})
        self.assertEqual(before, {path: (self.root / path).read_bytes() for path in FILES})
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])
        report = format_rehearsal_markdown(data)
        self.assertIn("2 of 5 checked call site(s) would break", report)
        self.assertIn("nothing was edited", report)
        self.assertIn("not proof of safety", report)

    def test_caller_patch_renames_exact_keywords_including_multiline_and_non_ascii_lines(self):
        patch = self.rehearse("auth/tokens.py::verify_token", "(token, scope=None)")["caller_patch"]
        self.assertEqual((patch["files"], patch["calls"], patch["skipped"]), (["api.py", "checkout.py"], 2, []))
        self.assertTrue(patch["patch"].startswith("*** Begin Patch\n*** Update File: api.py\n@@\n"))
        self.assertTrue(patch["patch"].endswith("\n*** End Patch"))
        self.assertIn("-result = tokens.verify_token('t', audience='api')  # café\n"
                      "+result = tokens.verify_token('t', scope='api')  # café\n", patch["patch"])
        self.assertIn("-        audience='checkout',\n+        scope='checkout',\n", patch["patch"])
        # No patch is offered for a change that is not one mechanical rename.
        self.assertIsNone(self.rehearse("auth/tokens.py::verify_token", "(token, *, audience)")["caller_patch"])

    def test_prediction_equals_what_the_edit_guard_reports_afterwards(self):
        proposals = {
            "(token, scope=None)": "def verify_token(token, scope=None):\n    return token\n",
            "(token, *, audience)": "def verify_token(token, *, audience):\n    return token\n",
            "(token, audience=None, region=None)": "def verify_token(token, audience=None, region=None):\n    return token\n",
            "(token, /, audience=None)": "def verify_token(token, /, audience=None):\n    return token\n",
        }
        for number, (params, definition) in enumerate(proposals.items()):
            with self.subTest(params=params):
                # A fresh workspace per proposal: each edit starts from the original contract.
                root, db = Path(self.tmp.name) / f"repo{number}", Path(self.tmp.name) / f"index{number}.db"
                for path, text in FILES.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text, encoding="utf-8")
                index_workspace(root, db_path=db)
                predicted = rehearse_signature_change(root, "auth/tokens.py::verify_token", params, db_path=db)
                (root / "auth/tokens.py").write_text(definition + "\n" + TOKENS.split("\n\n", 1)[1], encoding="utf-8")
                alerts = reindex_edited_file(root, "auth/tokens.py", db_path=db)["drift_alerts"]
                reported = self.sites([c for a in alerts if a["severity"] == "critical" for c in a["affected_callers"]])
                self.assertEqual(self.sites(predicted["breaks"]), reported)
                # A proposal the rehearsal calls safe raises no critical alert when made for real.
                self.assertEqual(bool(predicted["breaks"]), any(a["severity"] == "critical" for a in alerts))

    def test_constructors_and_class_methods_accept_a_proposal_written_with_its_receiver(self):
        for target in ("models.py::Client", "models.py::Client.__init__"):
            data = self.rehearse(target, "(self, host, deadline=5)")
            self.assertEqual(data["symbol"]["qualified_name"], "Client")
            self.assertEqual(data["current_signature"], "def __init__(self, host, timeout = 5)")
            self.assertEqual(self.sites(data["breaks"]), ["use_models.py:2"])
            self.assertIn("+Client('h', deadline=1)", data["caller_patch"]["patch"])
        built = self.rehearse("models.py::Client.build", "(cls, raw, *, issuer)")
        self.assertEqual(self.sites(built["breaks"]), ["use_models.py:3"])
        self.assertEqual(built["breaks"][0]["incompatibility"], "missing required keyword-only argument 'issuer'")
        self.assertEqual(self.rehearse("models.py::Client.build", "(cls, raw, issuer=None)")["breaks"], [])

    def test_unprovable_ambiguous_and_malformed_requests_do_not_guess(self):
        data = self.rehearse("models.py::Record", "(a)")
        self.assertFalse(data["ok"])
        self.assertIn("no statically proven calling convention", data["error"])
        self.assertIn("dataclass", data["error"])
        # A plain method has a contract, checked for calls on instances built in view.
        self.assertEqual(self.rehearse("models.py::Client.send", "(self, body, encoding)")["checked_call_sites"], 0)
        ambiguous = self.rehearse("verify_token", "(token)")
        self.assertFalse(ambiguous["ok"])
        self.assertEqual(ambiguous["candidates"], ["auth/tokens.py::verify_token", "other.py::verify_token"])
        self.assertIn("not valid Python", self.rehearse("auth/tokens.py::verify_token", "(token,, x)")["error"])
        self.assertIn("in parentheses", self.rehearse("auth/tokens.py::verify_token", "token, scope")["error"])
        self.assertIn("No symbol", self.rehearse("missing_symbol", "(a)")["error"])
        self.assertIn("exceeds", self.rehearse("auth/tokens.py::verify_token", "(" + "a, " * 900 + "b)")["error"])

    def test_patch_is_withheld_for_a_caller_changed_since_indexing(self):
        (self.root / "api.py").write_text("import auth.tokens as tokens\n\nresult = tokens.verify_token('t', audience='api')\n", encoding="utf-8")
        patch = self.rehearse("auth/tokens.py::verify_token", "(token, scope=None)")["caller_patch"]
        self.assertEqual(patch["files"], ["checkout.py"])
        self.assertEqual(patch["skipped"][0]["path"], "api.py")
        self.assertIn("changed since it was indexed", patch["skipped"][0]["reason"])
        self.assertNotIn("api.py", patch["patch"])


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "locus_rehearsal_pkg", str(REPO_ROOT / "__init__.py"), submodule_search_locations=[str(REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(REPO_ROOT)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _StubCtx:
    def __init__(self, cwd: str) -> None:
        self._cwd, self.refs, self.tools, self.commands = cwd, {}, {}, {}

    def session_cwd(self) -> str:
        return self._cwd

    def get_config(self, key: str, default: Any = None) -> Any:
        return default

    def register_context_reference(self, provider: Any) -> None:
        self.refs[provider.prefix] = provider

    def register_tool(self, name: str, toolset: str, schema: dict, handler: Any, **kwargs: Any) -> None:
        self.tools[name] = (schema, handler)

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.commands[name] = handler

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


class RehearsalSurfaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_reference_slash_and_cli_share_one_rehearsal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            for path, text in FILES.items():
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
            previous = os.environ.get("LOCUS_DB_PATH")
            os.environ["LOCUS_DB_PATH"] = str(Path(tmp) / "surface.db")
            try:
                plugin = _load_plugin_module()
                ctx = _StubCtx(str(root))
                plugin.register(ctx)
                schema, handler = ctx.tools["locus_rehearse"]
                self.assertEqual(schema["parameters"]["required"], ["target", "new_signature"])
                result = json.loads(handler({"target": "auth/tokens.py::verify_token", "new_signature": "(token, scope=None)"}))
                self.assertTrue(result["ok"])
                self.assertEqual(result["would_break"], ["api.py:2", "checkout.py:4"])
                self.assertEqual(result["caller_patch_files"], ["api.py", "checkout.py"])
                self.assertIn("*** Begin Patch", result["caller_patch"])
                # A model may also pass the whole definition line or omit the parentheses.
                for target, proposed in (("auth/tokens.py::verify_token", "def verify_token(token, scope=None) -> str:"),
                                         ("auth/tokens.py::verify_token", "token, scope=None")):
                    self.assertEqual(json.loads(handler({"target": target, "new_signature": proposed}))["would_break"],
                                     ["api.py:2", "checkout.py:4"])
                self.assertFalse(json.loads(handler({"target": "", "new_signature": "(a)"}))["ok"])
                self.assertEqual(len(json.loads(handler({"target": "verify_token", "new_signature": "(a)"}))["candidates"]), 2)

                reference = ctx.refs["whatif"]
                expanded = await reference.expand("auth/tokens.py::verify_token(token,*,scope=None)")
                self.assertIn("2 of 5 checked call site(s) would break", expanded)
                self.assertIn("Give a symbol and its proposed parameters", await reference.expand("verify_token"))
                completions = await reference.autocomplete("verify_tok")
                self.assertIn("auth/tokens.py::verify_token", [item.text for item in completions])

                slash = ctx.commands["locus"]("whatif models.py::Client(self, host, deadline=5)")
                self.assertIn("`use_models.py:2`", slash)
                self.assertIn("Usage:", ctx.commands["locus"]("whatif"))

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = plugin._handle_cli_command(argparse.Namespace(
                        action="whatif", target="auth/tokens.py::verify_token(token, scope=None)",
                        workspace=str(root), as_json=True))
                self.assertEqual(code, 0)
                self.assertEqual(len(json.loads(output.getvalue())["breaks"]), 2)
                self.assertEqual({path: (root / path).read_text(encoding="utf-8") for path in FILES}, FILES)
            finally:
                if previous is None:
                    os.environ.pop("LOCUS_DB_PATH", None)
                else:
                    os.environ["LOCUS_DB_PATH"] = previous


if __name__ == "__main__":
    unittest.main()
