"""Precise imported Python bindings and conservative ambiguity boundaries."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from locus_core import get_hotspots_report, index_workspace, reindex_edited_file
from locus_core.storage import open_locus_db


class PythonResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def check_edit(self, files, target="lib.py", after="def prepare(new): pass\n"):
        for path, text in files.items():
            self.write(path, text)
        index_workspace(self.root, db_path=self.db)
        self.write(target, after)
        return reindex_edited_file(self.root, target, db_path=self.db)["drift_alerts"]

    def test_module_aliases_and_direct_aliases_preserve_binding_and_repair(self):
        calls = {
            "plain.py": "import lib\nlib.prepare(old=1)\n",
            "alias.py": "import lib as service\nservice.prepare(old=1)\n",
            "direct.py": "from lib import prepare as invoke\ninvoke(old=1)\n",
        }
        alerts = self.check_edit({"lib.py": "def prepare(old): pass\n", **calls})
        self.assertEqual({c["path"] for c in alerts[0]["affected_callers"]}, set(calls))
        self.assertTrue(all("unexpected keyword" in c["incompatibility"] for c in alerts[0]["affected_callers"]))
        for path, text in calls.items():
            self.write(path, text.replace("old=", "new="))
            reindex_edited_file(self.root, path, db_path=self.db)
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_src_layout_absolute_relative_and_static_reexports(self):
        files = {
            "src/pkg/__init__.py": "from .api import prepare as exported\nfrom . import api\n",
            "src/pkg/api.py": "def prepare(old): pass\n",
            "src/pkg/relative.py": "from . import api as service\nservice.prepare(old=1)\n",
            "src/pkg/nested/consumer.py": "from ..api import prepare as invoke\ninvoke(old=1)\n",
            "module.py": "import pkg.api\npkg.api.prepare(old=1)\n",
            "alias.py": "import pkg.api as service\nservice.prepare(old=1)\n",
            "from.py": "from pkg import api\napi.prepare(old=1)\n",
            "reexport.py": "import pkg\npkg.exported(old=1)\n",
            "reexport_alias.py": "from pkg import exported as invoke\ninvoke(old=1)\n",
        }
        alerts = self.check_edit(files, "src/pkg/api.py")
        self.assertEqual({c["path"] for c in alerts[0]["affected_callers"]}, set(files) - {"src/pkg/__init__.py", "src/pkg/api.py"})

    def test_function_local_imports_and_unrelated_shadow_are_independent(self):
        caller = """from lib import prepare

def shadow(prepare):
    prepare(old=1)

def first():
    from lib import prepare as run
    run(old=1)

def second():
    from lib import prepare as run
    run(old=1)

def third():
    prepare(old=1)

def unrelated_dynamic_lookup():
    return globals().get('optional_feature')
"""
        alerts = self.check_edit({"lib.py": "def prepare(old): pass\n", "caller.py": caller})
        self.assertEqual([c["caller"] for c in alerts[0]["affected_callers"]], ["first", "second", "third"])

    def test_shadowing_mutation_and_conditional_bindings_do_not_guess(self):
        cases = {
            "parameter": "import lib\ndef use(lib):\n    lib.prepare(old=1)\n",
            "assignment": "import lib\nlib = object()\nlib.prepare(old=1)\n",
            "later_assignment": "import lib\ndef use():\n    lib.prepare(old=1)\n    lib = object()\n",
            "conditional": "if condition:\n    import lib\nlib.prepare(old=1)\n",
            "try_import": "try:\n    import lib\nexcept ImportError:\n    pass\nlib.prepare(old=1)\n",
            "before_import": "lib.prepare(old=1)\nimport lib\n",
            "unknown_receiver": "from lib import prepare\nservice.prepare(old=1)\n",
            "external": "from external import prepare\nprepare(old=1)\n",
            "star": "from lib import prepare\nfrom external import *\nprepare(old=1)\n",
            "module_write": "import lib\nlib.prepare = custom\nlib.prepare(old=1)\n",
            "setattr": "import lib\nsetattr(lib, 'prepare', custom)\nlib.prepare(old=1)\n",
            "global": "import lib\ndef mutate():\n    global lib\n    lib = custom\nlib.prepare(old=1)\n",
            "lambda": "import lib\n(lambda lib: lib.prepare(old=1))(other)\n",
            "comprehension": "import lib\n[lib.prepare(old=1) for lib in items]\n",
            "walrus": "import lib\n[(lib := other) for item in items]\nlib.prepare(old=1)\n",
            "class_method": "class C:\n    import lib\n    def method(self):\n        lib.prepare(old=1)\n",
            "function_alias": "from lib import prepare\ninvoke = prepare\ninvoke(old=1)\n",
        }
        files = {f"{key}.py": text for key, text in cases.items()}
        files["lib.py"] = "def prepare(old): pass\n"
        self.assertEqual(self.check_edit(files), [])
        with open_locus_db(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM calls WHERE resolved_path = 'lib.py'").fetchone()[0], 0)

    def test_duplicate_modules_do_not_pick_a_source_root(self):
        self.assertEqual(self.check_edit({
            "lib.py": "def prepare(old): pass\n",
            "src/lib.py": "def prepare(old): pass\n",
            "caller.py": "import lib\nlib.prepare(old=1)\n",
        }), [])

    def test_package_defined_object_does_not_become_same_named_submodule(self):
        self.assertEqual(self.check_edit({
            "pkg/__init__.py": "api = object()\n",
            "pkg/api.py": "def prepare(old): pass\n",
            "caller.py": "from pkg import api\napi.prepare(old=1)\n",
        }, "pkg/api.py"), [])

    def test_conditional_and_cyclic_reexports_are_unknown(self):
        self.assertEqual(self.check_edit({
            "lib.py": "def prepare(old): pass\n",
            "public.py": "if condition:\n    from lib import prepare\n",
            "caller.py": "import public\npublic.prepare(old=1)\n",
            "cycle_a.py": "from cycle_b import prepare\n",
            "cycle_b.py": "from cycle_a import prepare\n",
            "cycle_caller.py": "import cycle_a\ncycle_a.prepare(old=1)\n",
        }), [])

    def test_module_deletion_stays_open_until_call_removed(self):
        self.check_edit({"lib.py": "def prepare(old): pass\n", "caller.py": "import lib\nlib.prepare(old=1)\n"}, after="")
        alerts = get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]
        self.assertEqual(alerts[0]["affected_callers"][0]["path"], "caller.py")
        (self.root / "lib.py").unlink()
        reindex_edited_file(self.root, "lib.py", db_path=self.db)
        self.assertEqual(len(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]), 1)
        self.write("caller.py", "import lib\n")
        reindex_edited_file(self.root, "caller.py", db_path=self.db)
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_decorated_function_has_no_proven_calling_convention(self):
        self.assertEqual(self.check_edit({
            "lib.py": "@decorate\ndef prepare(old): pass\n",
            "caller.py": "import lib\nlib.prepare(old=1)\n",
        }, after="@decorate\ndef prepare(new, extra): pass\n"), [])

    def test_scan_status_persists_across_incremental_failure_repair_and_cached_scan(self):
        self.write("a.py", "def prepare(old): pass\n")
        self.write("b.py", "pass\n")
        index_workspace(self.root, max_files=1, db_path=self.db)
        self.write("a.py", "def prepare(\n")
        reindex_edited_file(self.root, "a.py", db_path=self.db)
        def status():
            with open_locus_db(self.db) as conn:
                return json.loads(conn.execute("SELECT scan_status_json FROM workspaces").fetchone()[0])
        self.assertTrue(status()["truncated"])
        self.assertEqual(status()["parse_errors"][0]["path"], "a.py")
        self.write("a.py", "def prepare(new): pass\n")
        reindex_edited_file(self.root, "a.py", db_path=self.db)
        self.assertTrue(status()["truncated"])
        self.assertEqual(status()["parse_errors"], [])
        index_workspace(self.root, max_files=10, db_path=self.db)
        cached = index_workspace(self.root, max_files=10, db_path=self.db)
        self.assertEqual(cached["updated_files"], 0)
        self.assertFalse(status()["truncated"])
        self.assertEqual(status()["indexed_files"], 2)
