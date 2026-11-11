"""Constructor and class-bound method calls resolved without receiver type inference."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from locus_core import (
    analyze_impact, get_hotspots_report, index_workspace, lookup_symbol_details, reindex_edited_file,
)
from locus_core.storage import open_locus_db


class ClassBoundCallTests(unittest.TestCase):
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

    def edit(self, path, text):
        self.write(path, text)
        return reindex_edited_file(self.root, path, db_path=self.db)["drift_alerts"]

    def check_edit(self, files, after, target="lib.py"):
        for path, text in files.items():
            self.write(path, text)
        index_workspace(self.root, db_path=self.db)
        return self.edit(target, after)

    def open_alerts(self):
        return get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]

    def test_constructor_calls_bind_to_init_and_clear_after_repair(self):
        calls = {
            "direct.py": "from lib import Client\nClient('h', timeout=1)\n",
            "alias.py": "from lib import Client as C\ndef build():\n    return C('h', timeout=1)\n",
            "module.py": "import lib\nlib.Client('h', timeout=1)\n",
            "reexport.py": "from public import Api\nApi('h', timeout=1)\n",
        }
        alerts = self.check_edit(
            {"lib.py": "class Client:\n    def __init__(self, host, timeout=5):\n        pass\n",
             "public.py": "from lib import Client as Api\n", **calls},
            "class Client:\n    def __init__(self, host, deadline=5):\n        pass\n",
        )
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual((alert["symbol_name"], alert["severity"]), ("Client", "critical"))
        self.assertEqual(alert["old_signature"], "def __init__(self, host, timeout = 5)")
        self.assertEqual(alert["new_signature"], "def __init__(self, host, deadline = 5)")
        self.assertEqual({c["path"] for c in alert["affected_callers"]}, set(calls))
        for caller in alert["affected_callers"]:
            self.assertEqual(caller["incompatibility"], "unexpected keyword argument 'timeout'")
            self.assertEqual(caller["suggestion"], "Review replacing timeout= with deadline=.")
        for path, text in calls.items():
            self.edit(path, text.replace("timeout=", "deadline="))
        self.assertEqual(self.open_alerts(), [])

    def test_static_and_class_methods_bind_through_class_and_module(self):
        calls = {
            "direct.py": "from lib import Token\nToken.parse('x')\nToken.build('x')\n",
            "module.py": "import lib as tokens\ntokens.Token.parse('x')\n",
            "package.py": "from pkg import lib\nlib.Token.build('x')\n",
        }
        before = ("class Token:\n    @staticmethod\n    def parse(raw):\n        pass\n"
                  "    @classmethod\n    def build(cls, raw):\n        pass\n")
        for path in ("lib.py", "pkg/lib.py"):
            self.write(path, before)
        self.write("pkg/__init__.py", "")
        after = before.replace("def parse(raw)", "def parse(raw, strict)").replace(
            "def build(cls, raw)", "def build(cls, raw, *, issuer)")
        self.check_edit(calls, after)
        alerts = {a["symbol_name"]: a for a in self.open_alerts()}
        self.assertEqual(set(alerts), {"Token.parse", "Token.build"})
        self.assertEqual({(c["path"], c["line"]) for c in alerts["Token.parse"]["affected_callers"]},
                         {("direct.py", 2), ("module.py", 2)})
        self.assertEqual(alerts["Token.build"]["affected_callers"][0]["incompatibility"],
                         "missing required keyword-only argument 'issuer'")
        self.assertEqual([c["path"] for c in self.edit("pkg/lib.py", after)[0]["affected_callers"]], ["package.py"])

    def test_method_never_borrows_callers_of_a_same_named_function_or_class(self):
        files = {
            "lib.py": ("def make(a):\n    pass\n"
                       "class First:\n    @staticmethod\n    def make(a):\n        pass\n"
                       "class Second:\n    @staticmethod\n    def make(a):\n        pass\n"),
            "function.py": "from lib import make\nmake(1)\n",
            "first.py": "from lib import First\nFirst.make(1)\n",
            "second.py": "from lib import Second\nSecond.make(1)\n",
        }
        changed = files["lib.py"].replace("class First:\n    @staticmethod\n    def make(a)",
                                          "class First:\n    @staticmethod\n    def make(a, b)")
        alerts = self.check_edit(files, changed)
        self.assertEqual([(a["symbol_name"], [c["path"] for c in a["affected_callers"]]) for a in alerts],
                         [("First.make", ["first.py"])])
        details = lookup_symbol_details(self.root, "lib.py::First.make", db_path=self.db)
        self.assertEqual([c["path"] for c in details["callers"]], ["first.py"])
        impact = analyze_impact(self.root, "lib.py::Second.make", db_path=self.db)
        self.assertEqual([c["caller_path"] for c in impact["external_call_sites"]], ["second.py"])
        # Removing one method does not report importers of the same-named function.
        removed = changed.replace("class Second:\n    @staticmethod\n    def make(a):\n        pass\n",
                                  "class Second:\n    pass\n")
        alerts = {a["symbol_name"]: a for a in self.edit("lib.py", removed)}
        self.assertEqual([c["path"] for c in alerts["Second.make"]["affected_callers"]], ["second.py"])
        self.edit("second.py", "from lib import Second\n")
        self.assertEqual([a["symbol_name"] for a in self.open_alerts()], ["First.make"])

    def test_default_and_inherited_constructors(self):
        files = {
            "lib.py": ("class Plain:\n    pass\n"
                       "class Base:\n    def __init__(self, a):\n        pass\n"
                       "class Child(Base):\n    limit = 1\n"
                       "class Failure(ValueError):\n    def __init__(self, code):\n        pass\n"),
            "caller.py": "import lib\nlib.Plain()\nlib.Child(1)\nlib.Failure(2)\nlib.Base(a=1)\n",
        }
        after = ("class Plain:\n    def __init__(self, a):\n        pass\n"
                 "class Base:\n    def __init__(self, a, b):\n        pass\n"
                 "class Child(Base):\n    limit = 1\n"
                 "class Failure(ValueError):\n    def __init__(self, code, detail):\n        pass\n")
        alerts = {a["symbol_name"]: a["affected_callers"] for a in self.check_edit(files, after)}
        self.assertEqual({name: [c["line"] for c in callers] for name, callers in alerts.items()},
                         {"Plain": [2], "Base": [5], "Child": [3], "Failure": [4]})

    def test_unprovable_classes_and_receivers_do_not_guess(self):
        cases = {
            "decorated": "@dataclass\nclass S:\n    def __init__(self, a):\n        pass\n",
            "metaclass": "class S(metaclass=Meta):\n    def __init__(self, a):\n        pass\n",
            "custom_new": "class S:\n    def __new__(cls, *a):\n        return 1\n    def __init__(self, a):\n        pass\n",
            "imported_base": "from other import Base\nclass S(Base):\n    def __init__(self, a):\n        pass\n",
            "enum": "import enum\nclass S(enum.Enum):\n    def __init__(self, a):\n        pass\n",
            "bare_exception": "class S(Exception):\n    pass\n",
            "conditional_init": "class S:\n    if flag:\n        def __init__(self, a):\n            pass\n",
            "rebound_init": "class S:\n    def __init__(self, a):\n        pass\n    __init__ = other\n",
            "decorated_init": "class S:\n    @wrap\n    def __init__(self, a):\n        pass\n",
            "rebound_class": "class S:\n    def __init__(self, a):\n        pass\nS = wrap(S)\n",
            "patched_class": "class S:\n    def __init__(self, a):\n        pass\nS.__init__ = other\n",
            "shadowed_builtin": "class Exception:\n    pass\nclass S(Exception):\n    pass\n",
        }
        files, edits = {}, {}
        for name, source in cases.items():
            files[f"{name}.py"] = source
            edits[f"{name}.py"] = source.replace("(self, a)", "(self, a, b)") + "\n"
            files[f"use_{name}.py"] = f"from {name} import S\nS(1)\n"
        methods = {
            "instance": "class S:\n    def run(self, a):\n        pass\n",
            "property_like": "class S:\n    @cached\n    def run(a):\n        pass\n",
            "shadowed_static": "staticmethod = wrap\nclass S:\n    @staticmethod\n    def run(a):\n        pass\n",
            "rebound_member": "class S:\n    @staticmethod\n    def run(a):\n        pass\n    run = other\n",
            "imported_parent": "from other import Base\nclass S(Base):\n    pass\n",
        }
        for name, source in methods.items():
            files[f"{name}.py"] = source
            edits[f"{name}.py"] = source.replace("(self, a)", "(self, a, b)").replace("run(a)", "run(a, b)") + "\n"
            files[f"use_{name}.py"] = f"from {name} import S\nS.run(1)\ns = S()\ns.run(1)\nS().run(1)\n"
        for path, text in files.items():
            self.write(path, text)
        index_workspace(self.root, db_path=self.db)
        for path, text in edits.items():
            self.assertEqual(self.edit(path, text), [], path)
        self.assertEqual(self.open_alerts(), [])
        with open_locus_db(self.db) as conn:
            bound = {row[0] for row in conn.execute("SELECT callee_qualname FROM calls WHERE callee_qualname LIKE '%.%'")}
        self.assertEqual(bound, set())

    def test_inherited_static_method_follows_single_local_base(self):
        files = {
            "lib.py": ("class Base:\n    @staticmethod\n    def load(a):\n        pass\n"
                       "class Child(Base):\n    @staticmethod\n    def save(a):\n        pass\n"),
            "caller.py": "from lib import Child\nChild.load(1)\nChild.save(1)\n",
        }
        alerts = self.check_edit(files, files["lib.py"].replace("def load(a)", "def load(a, b)"))
        self.assertEqual([(a["symbol_name"], [c["line"] for c in a["affected_callers"]]) for a in alerts],
                         [("Base.load", [2])])
        self.edit("caller.py", "from lib import Child\nChild.load(1, 2)\nChild.save(1)\n")
        self.assertEqual(self.open_alerts(), [])
        # Pulling a method up into the base keeps the call valid: no removal claim.
        pulled_up = ("class Base:\n    @staticmethod\n    def load(a, b):\n        pass\n"
                     "    @staticmethod\n    def save(a):\n        pass\n"
                     "class Child(Base):\n    pass\n")
        self.assertEqual(self.edit("lib.py", pulled_up), [])
        self.assertEqual(self.open_alerts(), [])

    def test_removed_class_and_member_stay_open_until_callers_change(self):
        files = {
            "lib.py": "class Store:\n    def __init__(self, a):\n        pass\n    @staticmethod\n    def open(a):\n        pass\n",
            "caller.py": "import lib\nlib.Store(1)\nlib.Store.open(1)\n",
        }
        alerts = self.check_edit(files, "class Store:\n    def __init__(self, a):\n        pass\n")
        self.assertEqual([(a["symbol_name"], a["severity"], a["new_signature"]) for a in alerts],
                         [("Store.open", "critical", "<removed>")])
        index_workspace(self.root, force=True, db_path=self.db)
        self.assertEqual(self.open_alerts()[0]["affected_callers"][0]["line"], 3)
        self.edit("lib.py", "VALUE = 1\n")
        by_name = {a["symbol_name"]: a for a in self.open_alerts()}
        self.assertEqual([c["line"] for c in by_name["Store"]["affected_callers"]], [2])
        self.assertEqual(by_name["Store.open"]["affected_callers"][0]["analysis_status"], "needs_verification")
        self.edit("caller.py", "import lib\n")
        self.assertEqual(self.open_alerts(), [])


if __name__ == "__main__":
    unittest.main()
