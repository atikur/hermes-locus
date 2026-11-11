"""Method calls on a name assigned once from a proven class constructor."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from locus_core import get_hotspots_report, index_workspace, rehearse_signature_change, reindex_edited_file
from locus_core.storage import open_locus_db

LIB = (
    "class Base:\n"
    "    def ping(self, target):\n        return target\n"
    "class Client(Base):\n"
    "    def __init__(self, host):\n        self.host = host\n"
    "    def send(self, body, retries=1):\n        return body\n"
    "    @staticmethod\n    def parse(raw):\n        return raw\n"
    "    @classmethod\n    def build(cls, raw):\n        return cls(raw)\n"
    "def own():\n    client = Client('h')\n    return client.send('b', retries=2)\n"
)
CHANGED = (LIB.replace("def send(self, body, retries=1)", "def send(self, body, attempts=1)")
           .replace("def ping(self, target)", "def ping(self, target, timeout)")
           .replace("def parse(raw)", "def parse(raw, strict)"))


class InstanceCallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "repo"
        self.root.mkdir()
        self.db = Path(self.tmp.name) / "index.db"

    def start(self, files):
        for path, text in files.items():
            (self.root / path).write_text(text, encoding="utf-8")
        index_workspace(self.root, db_path=self.db)

    def edit(self, path, text):
        (self.root / path).write_text(text, encoding="utf-8")
        return reindex_edited_file(self.root, path, db_path=self.db)["drift_alerts"]

    def named(self, alerts):
        return {a["symbol_name"]: sorted((c["path"], c["line"]) for c in a["affected_callers"]) for a in alerts}

    def test_methods_on_a_locally_constructed_instance_bind_exactly(self):
        callers = {
            "direct.py": "from lib import Client\ndef use():\n    client = Client('h')\n    client.send('b', retries=3)\n    return client.ping('x')\n",
            "module.py": "import lib\nclient = lib.Client('h')\nclient.send('b', retries=3)\nclient.parse('x')\n",
            "alias.py": "from lib import Client as C\ndef use():\n    c: C = C('h')\n    return [c.send(item, retries=3) for item in 'ab']\n",
            "nested.py": "from lib import Client\ndef outer():\n    client = Client('h')\n    def inner():\n        return client.send('b', retries=3)\n    return inner\n",
        }
        self.start({"lib.py": LIB, **callers})
        alerts = self.edit("lib.py", CHANGED)
        self.assertEqual(self.named(alerts), {
            "Client.send": [("alias.py", 4), ("direct.py", 4), ("lib.py", 17), ("module.py", 3), ("nested.py", 5)],
            "Base.ping": [("direct.py", 5)],
            "Client.parse": [("module.py", 4)],
        })
        send = next(a for a in alerts if a["symbol_name"] == "Client.send")
        self.assertTrue(all(c["incompatibility"] == "unexpected keyword argument 'retries'" for c in send["affected_callers"]))
        self.assertTrue(all(c["rename"] == ["retries", "attempts"] for c in send["affected_callers"]))
        # The same contract is available before the edit, written as in source.
        (self.root / "lib.py").write_text(LIB, encoding="utf-8")
        index_workspace(self.root, force=True, db_path=self.db)
        rehearsal = rehearse_signature_change(self.root, "lib.py::Client.send", "(self, body, attempts=1)", db_path=self.db)
        self.assertEqual(len(rehearsal["breaks"]), 5)
        self.assertEqual(rehearsal["caller_patch"]["files"], ["alias.py", "direct.py", "lib.py", "module.py", "nested.py"])

    def test_repairing_instance_callers_clears_the_alert(self):
        self.start({"lib.py": LIB, "use.py": "from lib import Client\nclient = Client('h')\nclient.send('b', retries=3)\n"})
        self.edit("lib.py", CHANGED.replace("retries=2", "attempts=2"))
        self.assertEqual([a["symbol_name"] for a in get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"]], ["Client.send"])
        self.edit("use.py", "from lib import Client\nclient = Client('h')\nclient.send('b', attempts=3)\n")
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])

    def test_receivers_whose_exact_type_is_not_in_view_are_left_alone(self):
        head = "from lib import Client\n"
        receivers = {
            "parameter": "def use(client):\n    return client.send('b', retries=3)\n",
            "reassigned": "client = Client('h')\nclient = other()\nclient.send('b', retries=3)\n",
            "conditional_other": "client = Client('h')\nif flag:\n    client = other()\nclient.send('b', retries=3)\n",
            "function_result": "def make():\n    return Client('h')\nclient = make()\nclient.send('b', retries=3)\n",
            "factory_result": "client = Client.build('h')\nclient.send('b', retries=3)\n",
            "attribute": "class Holder:\n    def use(self):\n        self.client = Client('h')\n        return self.client.send('b', retries=3)\n",
            "context_manager": "with Client('h') as client:\n    client.send('b', retries=3)\n",
            "tuple_target": "client, other = Client('h'), 1\nclient.send('b', retries=3)\n",
            "before_assignment": "def use():\n    client.send('b', retries=3)\n    client = Client('h')\n",
            "patched_instance": "client = Client('h')\nclient.send = print\nclient.send('b', retries=3)\n",
            "global_rebind": "client = Client('h')\ndef swap():\n    global client\n    client = other()\nclient.send('b', retries=3)\n",
            "deleted": "client = Client('h')\ndel client\nclient.send('b', retries=3)\n",
            "through_class": "client = Client('h')\nClient.send(client, 'b', retries=3)\n",
            "shadowing_parameter": "client = Client('h')\ndef use(client):\n    return client.send('b', retries=3)\n",
        }
        files = {f"{name}.py": head + body for name, body in receivers.items()}
        self.start({"lib.py": LIB, **files})
        alerts = self.edit("lib.py", CHANGED)
        self.assertEqual(self.named(alerts).get("Client.send"), [("lib.py", 17)])
        with open_locus_db(self.db) as conn:
            bound = {row[0] for row in conn.execute(
                "SELECT caller_path FROM calls WHERE callee_qualname = 'Client.send' AND caller_path != 'lib.py'")}
        self.assertEqual(bound, set())

    def test_value_returned_by_a_factory_is_not_treated_as_an_instance(self):
        # Found on real code: `attempt = Attempt.resolve(...)` must not bind `attempt.run()` to resolve.
        source = ("class Attempt:\n"
                  "    @classmethod\n    def resolve(cls, request, metadata):\n        return cls()\n"
                  "    @staticmethod\n    def parse(text):\n        return {}\n"
                  "    def run(self, callback):\n        return callback\n"
                  "def use(request):\n"
                  "    attempt = Attempt.resolve(request, {})\n    attempt.run(print)\n"
                  "    tables = Attempt.parse('x')\n    return tables.items()\n")
        self.start({"lib.py": source, "other.py": "from lib import Attempt\nvalue = Attempt.resolve(1, 2)\nvalue.run(print)\n"})
        with open_locus_db(self.db) as conn:
            bound = sorted(tuple(row) for row in conn.execute(
                "SELECT caller_path, line_no, callee_qualname FROM calls WHERE callee_qualname != '' ORDER BY 1, 2"))
        self.assertEqual(bound, [("lib.py", 11, "Attempt.resolve"), ("lib.py", 13, "Attempt.parse"),
                                 ("other.py", 2, "Attempt.resolve")])
        changed = source.replace("def run(self, callback)", "def run(self, callback, extra)").replace(
            "def resolve(cls, request, metadata)", "def resolve(cls, request, metadata, *, mode)")
        self.assertEqual(self.named(self.edit("lib.py", changed)),
                         {"Attempt.resolve": [("lib.py", 11), ("other.py", 2)]})

    def test_classes_whose_instances_may_shadow_or_intercept_methods_are_left_alone(self):
        body = "    def send(self, body, retries=1):\n        return body\n"
        classes = {
            "instance_attribute": "class Client:\n    def __init__(self):\n        self.send = print\n" + body,
            "fallback_lookup": "class Client:\n    def __getattr__(self, name):\n        return print\n" + body,
            "dynamic_attributes": "class Client:\n    def __init__(self, **options):\n        for key in options:\n            setattr(self, key, options[key])\n" + body,
            "custom_new": "class Client:\n    def __new__(cls):\n        return object.__new__(Other)\n" + body,
            "metaclass": "class Client(metaclass=Meta):\n" + body,
            "decorated_class": "@dataclass\nclass Client:\n" + body,
            "imported_base": "from other import Base\nclass Client(Base):\n" + body,
            "decorated_method": "class Client:\n    @retry\n    def send(self, body, retries=1):\n        return body\n",
            "rebound_method": "class Client:\n" + body + "    send = wrap(send)\n",
        }
        files = {}
        for name, source in classes.items():
            files[f"{name}.py"] = source
            files[f"use_{name}.py"] = f"from {name} import Client\nclient = Client()\nclient.send('b', retries=3)\n"
        self.start(files)
        for name, source in classes.items():
            self.assertEqual(self.edit(f"{name}.py", source.replace("retries=1", "attempts=1")), [], name)
        self.assertEqual(get_hotspots_report(self.root, db_path=self.db)["open_drift_alerts"], [])


if __name__ == "__main__":
    unittest.main()
