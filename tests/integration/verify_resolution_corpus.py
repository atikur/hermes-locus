#!/usr/bin/env python3
"""Supplement the preserved 19-case corpus with resolution and false-positive checks.

No model calls. Real source projections keep pinned original callers. Separate
constructed controls execute in isolated Python processes to label binding and
non-target/shadowing behavior. This is a targeted regression set, not a blind or
representative held-out benchmark. Every case is declared before running Locus.

Protocol 2 adds class-bound calls in two further groups, leaving the original
two groups and their cases unchanged: constructors, static/class methods and
methods on locally constructed instances, on
pinned Hermes classes, and authored executable class fixtures. Breaks that the
documented limits exclude (instance receivers, decorated classes, imported
bases) stay in the denominator under their own scope instead of being omitted.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

from verify_hermes_refactor import WORKSPACE_COMMIT, engine_digest, extract_commit, git_head
from verify_signature_corpus import binding_error, callable_signature, definition, git, patch_text


PINS = {
    "agent/retry_utils.py": "fc1d702f263b243c4d1c708507a565411f9f6790",
    "agent/auxiliary_rate_limit_backoff.py": "27a5f0527c396b2b4d1d322e2b7fc6439fea3941",
    "tests/agent/test_retry_utils.py": "e090364669ea7a0e9d670e88dc8c126a5e32b00f",
    "agent/chat_completion_wait_notice.py": "5fc15959ccf740ac9b23816f89bbb5bb0012775f",
    "agent/chat_completion_nonstream.py": "62d5bd7af517815dc47009e2fe73488324bdb847",
    "tests/agent/test_codex_ttfb_watchdog.py": "7947a13f2f99a958167a22d3a044a6c019c10cb0",
    "agent/aux_accounting.py": "df0e1b1c6c7e0c7406d2e72960609fac0c66b6ca",
    "agent/auxiliary_client.py": "c7866a87a03e0283c41ec284ca66f21c96830013",
    "plugins/image_gen/_common.py": "8af6741ae47138021fec100c3d8f3628138348e1",
    "tests/hermes_state/test_aux_usage_accounting.py": "1d6a101b414f2d4ba4c90a9627eb2543581f30b5",
    # Protocol 2: class-bound targets.
    "tools/debug_helpers.py": "259a69075e1b2c6393fe16d1cef1ac2361bd700f",
    "tools/image_generation_tool.py": "08eb613126e545ee6f544e7c56f9e2016ba1ee63",
    "tools/vision_tools.py": "78d8a431f12c8e4889fc6b44215a3166050aa7fe",
    "tools/web_tools.py": "02b9ef3f3a8ddb933bd1fe7f093d43c456609e45",
    "tests/tools/test_debug_helpers.py": "eb4b1081a8e94850e38c43d72f923940ae61427b",
    "plugins/platforms/signal/signal_rate_limit.py": "320c61edaf1906dbc54b1abad7710fb1f5b7f408",
    "plugins/platforms/signal/adapter.py": "8f21db746e7092e8dfed6480c8be695b9d8c9896",
    "tests/gateway/test_signal.py": "5584f505701f0e06db636cf23767520b6c6e08a8",
    "hermes_cli/local_runtime/supervisor.py": "6fd9e6868df8c0b8f9296d890d7c1746cd356f10",
    "hermes_cli/local_runtime/bootstrap.py": "b23950044be2200f36d0936d558cdc5ba0ce085e",
    "hermes_cli/local_runtime/recovery.py": "f7a481be17302e92e7d550077f402d899d576e04",
    "tests/hermes_cli/test_local_runtime_recovery.py": "80836fa6283ec3c32d12e8df63336a8da330ffe2",
    "plugins/teams_pipeline/store.py": "1bc47c49224f2fdab0ce8eba361c0fee63937da9",
    "plugins/teams_pipeline/pipeline.py": "e18d89b56e2f26eafeafda161ee4e9c6d3854b5c",
    "tests/plugins/test_teams_pipeline_plugin.py": "606ebecd3fd108666e3cb755737dbed036be9dc7",
}
TARGETS = [
    {"name": "retry", "helper": "agent/retry_utils.py", "symbol": "jittered_backoff", "parameter": "max_delay",
     "files": ["agent/retry_utils.py", "agent/auxiliary_rate_limit_backoff.py", "tests/agent/test_retry_utils.py"],
     "sites": ["agent/retry_utils.py:167", "agent/auxiliary_rate_limit_backoff.py:45",
               "tests/agent/test_retry_utils.py:15", "tests/agent/test_retry_utils.py:24", "tests/agent/test_retry_utils.py:45"]},
    {"name": "watchdog", "helper": "agent/chat_completion_wait_notice.py", "symbol": "codex_watchdog_deadline",
     "parameter": "stale_timeout", "files": ["agent/chat_completion_wait_notice.py", "agent/chat_completion_nonstream.py",
                                               "tests/agent/test_codex_ttfb_watchdog.py"],
     "sites": ["agent/chat_completion_nonstream.py:168", "tests/agent/test_codex_ttfb_watchdog.py:335",
               "tests/agent/test_codex_ttfb_watchdog.py:458"]},
    {"name": "accounting", "helper": "agent/aux_accounting.py", "symbol": "record_aux_usage", "parameter": "provider",
     "files": ["agent/aux_accounting.py", "agent/auxiliary_client.py", "plugins/image_gen/_common.py",
               "tests/hermes_state/test_aux_usage_accounting.py"],
     "sites": ["agent/auxiliary_client.py:6647", "plugins/image_gen/_common.py:270",
               "tests/hermes_state/test_aux_usage_accounting.py:199", "tests/hermes_state/test_aux_usage_accounting.py:222",
               "tests/hermes_state/test_aux_usage_accounting.py:223"]},
]
# Class-bound targets: "owner" is the class, "symbol" the member whose signature
# changes. A changed ``__init__`` is the contract of calling the class itself.
CLASS_TARGETS = [
    {"name": "debug-session", "helper": "tools/debug_helpers.py", "owner": "DebugSession", "symbol": "__init__",
     "parameter": "env_var",
     "files": ["tools/debug_helpers.py", "tools/image_generation_tool.py", "tools/vision_tools.py",
               "tools/web_tools.py", "tests/tools/test_debug_helpers.py"],
     "sites": ["tools/image_generation_tool.py:48", "tools/vision_tools.py:59", "tools/web_tools.py:263",
               "tests/tools/test_debug_helpers.py:14", "tests/tools/test_debug_helpers.py:24"]},
    {"name": "signal-rate-limit", "helper": "plugins/platforms/signal/signal_rate_limit.py",
     "owner": "SignalRateLimitError", "symbol": "__init__", "parameter": "retry_after",
     "files": ["plugins/platforms/signal/signal_rate_limit.py", "plugins/platforms/signal/adapter.py",
               "tests/gateway/test_signal.py"],
     "sites": ["plugins/platforms/signal/adapter.py:618", "plugins/platforms/signal/adapter.py:625",
               "tests/gateway/test_signal.py:1145"]},
    {"name": "terminate-tree", "helper": "hermes_cli/local_runtime/supervisor.py",
     "owner": "LlamaServerSupervisor", "symbol": "_terminate_tree", "parameter": "verified_root",
     "files": ["hermes_cli/local_runtime/supervisor.py", "hermes_cli/local_runtime/bootstrap.py",
               "hermes_cli/local_runtime/recovery.py", "tests/hermes_cli/test_local_runtime_recovery.py"],
     "sites": ["hermes_cli/local_runtime/supervisor.py:301", "hermes_cli/local_runtime/bootstrap.py:183",
               "hermes_cli/local_runtime/recovery.py:194", "tests/hermes_cli/test_local_runtime_recovery.py:284",
               "tests/hermes_cli/test_local_runtime_recovery.py:286"]},
    {"name": "receipt-key", "helper": "plugins/teams_pipeline/store.py", "owner": "TeamsPipelineStore",
     "symbol": "build_notification_receipt_key", "parameter": "notification",
     "files": ["plugins/teams_pipeline/store.py", "plugins/teams_pipeline/pipeline.py",
               "tests/plugins/test_teams_pipeline_plugin.py"],
     "sites": ["plugins/teams_pipeline/pipeline.py:193", "plugins/teams_pipeline/pipeline.py:447",
               "tests/plugins/test_teams_pipeline_plugin.py:398"]},
]


def member_definition(source, owner, member):
    """Locate a class member whose binding a static oracle can state.

    Asserts ordinary construction and lookup: no class decorator, metaclass or
    ``__new__``, and only ``object``/``Exception`` bases.
    """
    classes = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == owner]
    assert len(classes) == 1, owner
    cls = classes[0]
    assert not cls.decorator_list and not cls.keywords, owner
    assert all(isinstance(base, ast.Name) and base.id in {"object", "Exception"} for base in cls.bases), owner
    assert not any(isinstance(node, ast.FunctionDef) and node.name == "__new__" for node in cls.body), owner
    members = [node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == member]
    assert len(members) == 1, (owner, member)
    decorators = [ast.unparse(item) for item in members[0].decorator_list]
    assert decorators == [] if member == "__init__" else decorators in (["staticmethod"], ["classmethod"]), decorators
    return members[0]


def member_signature(source, owner, member):
    """Parameters a caller binds: the declared ones without the implicit receiver."""
    node = copy.deepcopy(member_definition(source, owner, member))
    static = [ast.unparse(item) for item in node.decorator_list] == ["staticmethod"]
    node.decorator_list = []
    signature = callable_signature(ast.unparse(node), member)
    return signature if static else signature.replace(parameters=list(signature.parameters.values())[1:])


def mutated(source, symbol, parameter, kind, owner=None, preserve_lines=True):
    node = member_definition(source, owner, symbol) if owner else definition(source, symbol)
    revised = copy.deepcopy(node)
    if kind == "keyword-rename":
        for child in ast.walk(revised):
            if isinstance(child, ast.arg) and child.arg == parameter:
                child.arg = "renamed_" + parameter
            elif isinstance(child, ast.Name) and child.id == parameter:
                child.id = "renamed_" + parameter
    elif kind in {"required-keyword", "optional-keyword"}:
        revised.args.kwonlyargs.append(ast.arg(arg="corpus_probe"))
        revised.args.kw_defaults.append(None if kind == "required-keyword" else ast.Constant(None))
    elif kind == "body-only":
        revised.body.append(ast.Pass())
    else:
        raise ValueError(kind)
    lines = source.splitlines(keepends=True)
    # A method keeps its indentation; ast.unparse also emits its decorators.
    start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
    rendered = textwrap.indent(ast.unparse(revised), " " * node.col_offset) + "\n"
    old_line_count = node.end_lineno - start + 1
    assert len(rendered.splitlines()) <= old_line_count or not preserve_lines, "Preserve downstream caller locations in this corpus"
    rendered += "\n" * (old_line_count - len(rendered.splitlines()))
    return "".join(lines[:start - 1]) + rendered + "".join(lines[node.end_lineno:])


def real_cases(host):
    sources = {}
    for path, blob in PINS.items():
        raw = git(host, "show", f"{WORKSPACE_COMMIT}:{path}")
        assert hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest() == blob, path
        sources[path] = raw.decode()
    cases = []
    for target in TARGETS:
        files = {path: sources[path] for path in target["files"]}
        calls = {}
        for path, text in files.items():
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == target["symbol"]
                    or isinstance(node.func, ast.Attribute) and node.func.attr == target["symbol"]
                ):
                    site = f"{path}:{node.lineno}"
                    assert site not in calls
                    calls[site] = node
        assert set(calls) == set(target["sites"]), (target["name"], sorted(calls))
        before = files[target["helper"]]
        original_signature = callable_signature(before, target["symbol"])
        assert all(binding_error(original_signature, call) is None for call in calls.values())
        for kind in ("keyword-rename", "required-keyword", "optional-keyword", "body-only"):
            after = mutated(before, target["symbol"], target["parameter"], kind)
            signature = callable_signature(after, target["symbol"])
            cases.append({"case": f"hermes-{target['name']}-{kind}", "group": "pinned-real-source",
                          "files": files, "helper": target["helper"], "after": after,
                          "oracle": "CPython inspect.Signature.bind; original source call shapes, no Locus resolver",
                          "labels": {site: {"error": binding_error(signature, call),
                                            "scope": "same-file" if site.rsplit(":", 1)[0] == target["helper"] else "cross-file"}
                                     for site, call in calls.items()}})
    for target in CLASS_TARGETS:
        owner, symbol, helper = target["owner"], target["symbol"], target["helper"]
        files = {path: sources[path] for path in target["files"]}
        calls, scopes = {}, {}
        for path, text in files.items():
            for node in ast.walk(ast.parse(text)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if symbol == "__init__":
                    class_bound = (isinstance(func, ast.Name) and func.id == owner
                                   or isinstance(func, ast.Attribute) and func.attr == owner)
                    if not class_bound:
                        continue
                elif isinstance(func, ast.Attribute) and func.attr == symbol:
                    class_bound = (isinstance(func.value, ast.Name) and func.value.id == owner
                                   or isinstance(func.value, ast.Attribute) and func.value.attr == owner)
                else:
                    continue
                site = f"{path}:{node.lineno}"
                assert site not in calls
                calls[site] = node
                scopes[site] = "same-file" if path == helper else "cross-file" if class_bound else "instance-receiver"
        assert set(calls) == set(target["sites"]), (target["name"], sorted(calls))
        before = files[helper]
        end = member_definition(before, owner, symbol).end_lineno
        # Growing the definition is only safe when no labelled call follows it in the helper.
        preserve = any(site.rsplit(":", 1)[0] == helper and int(site.rsplit(":", 1)[1]) > end for site in calls)
        original_signature = member_signature(before, owner, symbol)
        assert all(binding_error(original_signature, call) is None for call in calls.values())
        for kind in ("keyword-rename", "required-keyword", "optional-keyword", "body-only"):
            after = mutated(before, symbol, target["parameter"], kind, owner=owner, preserve_lines=preserve)
            signature = member_signature(after, owner, symbol)
            cases.append({"case": f"hermes-{target['name']}-{kind}", "group": "pinned-real-class-source",
                          "files": files, "helper": helper, "after": after,
                          "oracle": "CPython inspect.Signature.bind on the member's declared parameters without the implicit "
                                    "receiver; the pinned class is asserted to have no decorator, metaclass or __new__; "
                                    "original source call shapes, no Locus resolver",
                          "labels": {site: {"error": binding_error(signature, call), "scope": scopes[site]}
                                     for site, call in calls.items()}})
    return cases


CONTROL_CALLERS = {
    "direct-alias": "from pkg.api import process as invoke\ndef run():\n    return invoke(1, limit=3)\n",
    "module-import": "import pkg.api\ndef run():\n    return pkg.api.process(1, limit=3)\n",
    "module-alias": "import pkg.api as api\ndef run():\n    return api.process(1, limit=3)\n",
    "from-module-alias": "from pkg import api as tools\ndef run():\n    return tools.process(1, limit=3)\n",
    "relative-module": "from . import api\ndef run():\n    return api.process(1, limit=3)\n",
    "repeated-local-import": "def first():\n    from pkg.api import process\n    return process(1, limit=3)\ndef second():\n    from pkg.api import process\n    return process(2, limit=3)\ndef run():\n    return first(), second()\n",
    "scoped-different-imports": "def first():\n    from pkg.api import process\n    return process(1, limit=3)\ndef second():\n    from pkg.other import process\n    return process(2, limit=3)\ndef run():\n    return first(), second()\n",
    "parameter-shadow": "from pkg import api\ndef invoke(api):\n    return api.process(1, limit=3)\ndef run():\n    from pkg import other\n    return invoke(other)\n",
    "assignment-shadow": "from pkg import api\nfrom pkg import other\napi = other\ndef run():\n    return api.process(1, limit=3)\n",
    "conditional-other-import": "if False:\n    from pkg.api import process\nelse:\n    from pkg.other import process\ndef run():\n    return process(1, limit=3)\n",
    "same-name-other-module": "from pkg.other import process\ndef run():\n    return process(1, limit=3)\n",
    "local-shadow": "from pkg.api import process\ndef run():\n    process = lambda value, *, limit=3: value + limit\n    return process(1, limit=3)\n",
    "unpacked-compatible": "from pkg.api import process\ndef run():\n    return process(*[1], **{'limit': 3})\n",
}


def executable_control(files, entry="run"):
    """Run only our tiny authored fixtures, never arbitrary upstream package code."""
    with tempfile.TemporaryDirectory(prefix="locus-binding-oracle-") as scratch:
        root = Path(scratch)
        for path, source in files.items():
            dest = root / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(source)
        assert entry in {"run", "first", "second"}
        code = "import json\ntry:\n from pkg import client\n value=getattr(client, " + repr(entry) + ")()\n print(json.dumps({'ok':True,'value':value}))\nexcept TypeError as exc:\n print(json.dumps({'ok':False,'error':str(exc)}))\n"
        result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                env={"PATH": os.environ.get("PATH", os.defpath), "PYTHONDONTWRITEBYTECODE": "1"},
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)


def control_cases():
    cases = []
    for name, caller in CONTROL_CALLERS.items():
        before = "def process(value, *, limit=3):\n    return value + limit\n"
        # Unpacked call is a compatible addition control, not a resolvability claim.
        after = ("def process(value, *, limit=3, extra=None):\n    return value + limit\n" if name == "unpacked-compatible"
                 else "def process(value, *, ceiling=3):\n    return value + ceiling\n")
        files = {"pkg/__init__.py": "", "pkg/api.py": before, "pkg/other.py": before, "pkg/client.py": caller}
        entries = ["first", "second"] if name in {"repeated-local-import", "scoped-different-imports"} else ["run"]
        original = {entry: executable_control(files, entry) for entry in entries}
        changed = {entry: executable_control({**files, "pkg/api.py": after}, entry) for entry in entries}
        assert all(result["ok"] for result in original.values()), name
        sites = [f"pkg/client.py:{node.lineno}" for node in ast.walk(ast.parse(caller))
                 if isinstance(node, ast.Call) and (
                     isinstance(node.func, ast.Name) and node.func.id in {"process", "invoke"}
                     and any(keyword.arg == "limit" for keyword in node.keywords)
                     or isinstance(node.func, ast.Attribute) and node.func.attr == "process"
                     or name == "unpacked-compatible" and isinstance(node.func, ast.Name) and node.func.id == "process")]
        assert sites, name
        sites.sort(key=lambda site: int(site.rsplit(":", 1)[1]))
        labels = {site: {"error": changed[entries[index] if len(entries) > 1 else "run"].get("error"),
                         "scope": "cross-file"} for index, site in enumerate(sites)}
        cases.append({"case": "control-" + name, "fixture_name": name, "group": "constructed-control", "files": files,
                      "helper": "pkg/api.py", "after": after,
                      "oracle": "Executed authored fixture before/after in clean Python subprocesses",
                      "oracle_execution": {"before": original, "after": changed},
                      "labels": labels})
    # Arity changes must also avoid warnings for unrelated shadowed functions.
    # These are additional mutations of the same fixtures, not new unique sites.
    for original_case in list(cases):
        name = original_case["fixture_name"]
        if name == "unpacked-compatible":
            continue
        after = "def process(value, *, limit=3, required_probe):\n    return value + limit\n"
        entries = ["first", "second"] if name in {"repeated-local-import", "scoped-different-imports"} else ["run"]
        changed = {entry: executable_control({**original_case["files"], "pkg/api.py": after}, entry) for entry in entries}
        sites = sorted(original_case["labels"], key=lambda site: int(site.rsplit(":", 1)[1]))
        labels = {site: {"error": changed[entries[index] if len(entries) > 1 else "run"].get("error"),
                         "scope": "cross-file"} for index, site in enumerate(sites)}
        cases.append({**original_case, "case": original_case["case"] + "-required-keyword", "after": after,
                      "oracle_execution": {"before": original_case["oracle_execution"]["before"], "after": changed},
                      "labels": labels})
    return cases


CLASS_API = """import dataclasses

from pkg.base import Root


def parse(raw):
    return raw


class Client:
    def __init__(self, host, timeout=5):
        self.host = host
        self.timeout = timeout

    @staticmethod
    def parse(raw):
        return raw

    @classmethod
    def build(cls, raw):
        return cls(raw).host

    def send(self, body):
        return body


class Child(Client):
    retries = 1


class Override(Client):
    def __init__(self, host, timeout=5):
        self.host = host

    @staticmethod
    def parse(raw):
        return raw


class Other:
    def __init__(self, host, timeout=5):
        self.host = host

    @staticmethod
    def parse(raw):
        return raw


class Plain:
    label = "plain"


class Failure(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _first_argument_only(cls):
    return lambda *args, **kwargs: cls(args[0])


@_first_argument_only
class Wrapped:
    def __init__(self, host, timeout=5):
        self.host = host


class _FirstArgumentOnly(type):
    def __call__(cls, *args, **kwargs):
        return super().__call__(args[0])


class Managed(metaclass=_FirstArgumentOnly):
    def __init__(self, host, timeout=5):
        self.host = host


class Replaced:
    def __new__(cls, *args, **kwargs):
        return object.__new__(Other)

    def __init__(self, host, timeout=5):
        self.host = host


class Patched:
    def __init__(self, host, timeout=5):
        self.host = host


def _original_init(self, host, timeout=5):
    self.host = host


Patched.__init__ = _original_init


@dataclasses.dataclass
class Record:
    host: str
    timeout: int = 5


class Derived(Root):
    pass
"""
CLASS_BASE = "class Root:\n    def __init__(self, host, timeout=5):\n        self.host = host\n"
_INIT = "    def __init__(self, host, timeout=5):"
_RENAMED = "    def __init__(self, host, deadline=5):"
# name: (edited file, text replaced exactly once, replacement)
CLASS_MUTATIONS = {
    "client-init-keyword-rename": ("pkg/api.py", "class Client:\n" + _INIT + "\n        self.host = host\n        self.timeout = timeout",
                                   "class Client:\n" + _RENAMED + "\n        self.host = host\n        self.timeout = deadline"),
    "client-init-required-keyword": ("pkg/api.py", "class Client:\n" + _INIT,
                                     "class Client:\n    def __init__(self, host, timeout=5, *, region):"),
    "client-parse-required": ("pkg/api.py", "self.timeout = timeout\n\n    @staticmethod\n    def parse(raw):",
                              "self.timeout = timeout\n\n    @staticmethod\n    def parse(raw, strict):"),
    "client-build-keyword-only": ("pkg/api.py", "    def build(cls, raw):", "    def build(cls, raw, *, issuer):"),
    "client-send-required": ("pkg/api.py", "    def send(self, body):", "    def send(self, body, encoding):"),
    "plain-gains-init": ("pkg/api.py", 'class Plain:\n    label = "plain"\n',
                         'class Plain:\n    label = "plain"\n\n    def __init__(self, host):\n        self.host = host\n'),
    "failure-init-required": ("pkg/api.py", "    def __init__(self, code):", "    def __init__(self, code, detail):"),
    "wrapped-init-keyword-rename": ("pkg/api.py", "class Wrapped:\n" + _INIT, "class Wrapped:\n" + _RENAMED),
    "managed-init-keyword-rename": ("pkg/api.py", "class Managed(metaclass=_FirstArgumentOnly):\n" + _INIT,
                                    "class Managed(metaclass=_FirstArgumentOnly):\n" + _RENAMED),
    "replaced-init-keyword-rename": ("pkg/api.py", "return object.__new__(Other)\n\n" + _INIT,
                                     "return object.__new__(Other)\n\n" + _RENAMED),
    "patched-init-keyword-rename": ("pkg/api.py", "class Patched:\n" + _INIT, "class Patched:\n" + _RENAMED),
    "record-field-rename": ("pkg/api.py", "    timeout: int = 5", "    deadline: int = 5"),
    "root-init-keyword-rename": ("pkg/base.py", _INIT, _RENAMED),
}
_CTOR = "    return {}('h', timeout=1).host  # site\n"
# fixture: (caller source with one "# site" call, mutation, scope, breaks?)
# Scopes other than cross-file are breaks outside the documented supported set.
CLASS_CONTROLS = {
    # Supported class-bound calls that the change breaks.
    "ctor-direct": ("from pkg.api import Client\ndef run():\n" + _CTOR.format("Client"), "client-init-keyword-rename", "cross-file", True),
    "ctor-direct-required": ("from pkg.api import Client\ndef run():\n" + _CTOR.format("Client"), "client-init-required-keyword", "cross-file", True),
    "ctor-alias": ("from pkg.api import Client as Api\ndef run():\n" + _CTOR.format("Api"), "client-init-keyword-rename", "cross-file", True),
    "ctor-module": ("import pkg.api\ndef run():\n" + _CTOR.format("pkg.api.Client"), "client-init-keyword-rename", "cross-file", True),
    "ctor-module-alias": ("import pkg.api as api\ndef run():\n" + _CTOR.format("api.Client"), "client-init-keyword-rename", "cross-file", True),
    "ctor-reexport": ("from pkg import Client\ndef run():\n" + _CTOR.format("Client"), "client-init-keyword-rename", "cross-file", True),
    "ctor-local-import": ("def run():\n    from pkg.api import Client\n" + _CTOR.format("Client"), "client-init-keyword-rename", "cross-file", True),
    "ctor-inherited": ("from pkg.api import Child\ndef run():\n" + _CTOR.format("Child"), "client-init-keyword-rename", "cross-file", True),
    "default-ctor": ("from pkg.api import Plain\ndef run():\n    return Plain().label  # site\n", "plain-gains-init", "cross-file", True),
    "exception-ctor": ("from pkg.api import Failure\ndef run():\n    try:\n        raise Failure(2)  # site\n    except Failure as error:\n        return error.code\n",
                       "failure-init-required", "cross-file", True),
    "static-direct": ("from pkg.api import Client\ndef run():\n    return Client.parse('x')  # site\n", "client-parse-required", "cross-file", True),
    "static-module-alias": ("import pkg.api as api\ndef run():\n    return api.Client.parse('x')  # site\n", "client-parse-required", "cross-file", True),
    "static-from-module": ("from pkg import api\ndef run():\n    return api.Client.parse('x')  # site\n", "client-parse-required", "cross-file", True),
    "static-inherited": ("from pkg.api import Child\ndef run():\n    return Child.parse('x')  # site\n", "client-parse-required", "cross-file", True),
    "classmethod-direct": ("from pkg.api import Client\ndef run():\n    return Client.build('x')  # site\n", "client-build-keyword-only", "cross-file", True),
    "classmethod-inherited": ("from pkg.api import Child\ndef run():\n    return Child.build('x')  # site\n", "client-build-keyword-only", "cross-file", True),
    # Calls the change leaves valid: another target, or construction that never binds the edited signature.
    "ctor-positional": ("from pkg.api import Client\ndef run():\n    return Client('h', 1).host  # site\n", "client-init-keyword-rename", "cross-file", False),
    "ctor-other-class": ("from pkg.api import Other\ndef run():\n" + _CTOR.format("Other"), "client-init-keyword-rename", "cross-file", False),
    "ctor-override": ("from pkg.api import Override\ndef run():\n" + _CTOR.format("Override"), "client-init-keyword-rename", "cross-file", False),
    "ctor-other-module": ("from pkg.other import Client\ndef run():\n" + _CTOR.format("Client"), "client-init-keyword-rename", "cross-file", False),
    "ctor-parameter-shadow": ("from pkg.api import Client\ndef build(Client):\n" + _CTOR.format("Client")
                              + "def run():\n    from pkg.other import Client as Unchanged\n    return build(Unchanged)\n",
                              "client-init-keyword-rename", "cross-file", False),
    "ctor-assignment-shadow": ("from pkg.api import Client\nfrom pkg import other\nClient = other.Client\ndef run():\n" + _CTOR.format("Client"),
                               "client-init-keyword-rename", "cross-file", False),
    "decorated-class": ("from pkg.api import Wrapped\ndef run():\n" + _CTOR.format("Wrapped"), "wrapped-init-keyword-rename", "cross-file", False),
    "metaclass-call": ("from pkg.api import Managed\ndef run():\n" + _CTOR.format("Managed"), "managed-init-keyword-rename", "cross-file", False),
    "custom-new": ("from pkg.api import Replaced\ndef run():\n    return type(Replaced('h', timeout=1)).__name__  # site\n",
                   "replaced-init-keyword-rename", "cross-file", False),
    "patched-init": ("from pkg.api import Patched\ndef run():\n" + _CTOR.format("Patched"), "patched-init-keyword-rename", "cross-file", False),
    "static-other-class": ("from pkg.api import Other\ndef run():\n    return Other.parse('x')  # site\n", "client-parse-required", "cross-file", False),
    "static-module-function": ("from pkg.api import parse\ndef run():\n    return parse('x')  # site\n", "client-parse-required", "cross-file", False),
    "static-override": ("from pkg.api import Override\ndef run():\n    return Override.parse('x')  # site\n", "client-parse-required", "cross-file", False),
    # Methods on a name assigned once from a proven constructor: the exact type is in view.
    "instance-method": ("from pkg.api import Client\ndef run():\n    client = Client('h')\n    return client.send('b')  # site\n",
                        "client-send-required", "cross-file", True),
    "instance-static": ("from pkg.api import Client\ndef run():\n    client = Client('h')\n    return client.parse('x')  # site\n",
                        "client-parse-required", "cross-file", True),
    # Real breaks outside the documented supported set: retained, not required.
    "instance-parameter": ("from pkg.api import Client\ndef use(client):\n    return client.send('b')  # site\ndef run():\n    return use(Client('h'))\n",
                           "client-send-required", "instance-receiver", True),
    "dataclass-ctor": ("from pkg.api import Record\ndef run():\n" + _CTOR.format("Record"), "record-field-rename", "decorated-class", True),
    "imported-base-ctor": ("from pkg.api import Derived\ndef run():\n" + _CTOR.format("Derived"), "root-init-keyword-rename", "imported-base", True),
}


def class_control_cases():
    """Authored class fixtures labelled only by executing them before and after the edit."""
    cases = []
    for name, (caller, mutation, scope, breaks) in CLASS_CONTROLS.items():
        helper, old, new = CLASS_MUTATIONS[mutation]
        files = {"pkg/__init__.py": "from .api import Client\n" if name == "ctor-reexport" else "",
                 "pkg/api.py": CLASS_API, "pkg/base.py": CLASS_BASE, "pkg/other.py": CLASS_API, "pkg/client.py": caller}
        assert files[helper].count(old) == 1, mutation
        after = files[helper].replace(old, new)
        original = executable_control(files)
        changed = executable_control({**files, helper: after})
        assert original["ok"], name
        # The fixture's intent is checked against execution; execution supplies the label.
        assert changed["ok"] is not breaks, (name, changed)
        marked = [number for number, line in enumerate(caller.splitlines(), 1) if line.endswith("# site")]
        assert len(marked) == 1, name
        cases.append({"case": f"class-control-{name}", "fixture_name": "class-" + name,
                      "group": "constructed-class-control", "mutation": mutation, "files": files,
                      "helper": helper, "after": after,
                      "oracle": "Executed authored fixture before/after in clean Python subprocesses",
                      "oracle_execution": {"before": {"run": original}, "after": {"run": changed}},
                      "labels": {f"pkg/client.py:{marked[0]}": {"error": changed.get("error"), "scope": scope}}})
    return cases


def score(rows):
    groups = {}
    for row in rows:
        key = row["group"]
        group = groups.setdefault(key, {"cases": 0, "broken": 0, "named_broken": 0, "compatible": 0,
                                        "named_compatible": 0, "unlabelled_named": 0, "missed_sites": [],
                                        "false_positive_sites": [], "same_file_broken": 0})
        group["cases"] += 1
        named = set(row["named_sites"])
        for site, label in row["labels"].items():
            if label["error"] is not None:
                group["broken"] += 1
                group["named_broken"] += site in named
                group["same_file_broken"] += label["scope"] == "same-file"
                if site not in named:
                    group["missed_sites"].append({"case": row["case"], "site": site, "scope": label["scope"]})
            else:
                group["compatible"] += 1
                group["named_compatible"] += site in named
                if site in named:
                    group["false_positive_sites"].append({"case": row["case"], "site": site})
        group["unlabelled_named"] += len(named - row["labels"].keys())
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--plugin-ref", default="HEAD", help="Git revision or WORKTREE; raw source manifest is always recorded.")
    parser.add_argument("--plugin-snapshot", type=Path, help="Already frozen candidate plugin tree.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true", help="Create and independently label the corpus without loading Locus.")
    parser.add_argument("--check-cross-file", action="store_true", help="Fail on missed cross-file calls or false positives; retain same-file misses explicitly.")
    args = parser.parse_args()
    args.source, args.runtime, args.output = (path.resolve() for path in (args.source, args.runtime, args.output))
    args.plugin_snapshot = args.plugin_snapshot.resolve() if args.plugin_snapshot else None
    assert not args.output.exists(), "Refuse to overwrite any prior evidence. Use a new output path."
    # Protocol 1 order first: the original pinned and authored cases keep their manifests.
    cases = real_cases(args.source)
    cases = ([case for case in cases if case["group"] == "pinned-real-source"] + control_cases()
             + [case for case in cases if case["group"] != "pinned-real-source"] + class_control_cases())
    report = {"protocol_version": 2, "model_calls": 0, "source_commit": WORKSPACE_COMMIT,
              "source_blob_pins": PINS, "selection": "Targeted extension declared before execution; not a blind held-out accuracy estimate.",
              "limits": ["Counts repeat source calls across mutations; report distinct locations separately.",
                         "Same-file calls remain in the denominator and are marked separately.",
                         "Constructed controls are separate from untouched real-source projections.",
                         "Class-bound groups label the member's parameters without the receiver; a non-cross-file scope "
                         "marks a real break outside the documented supported set (same file, instance receiver, "
                         "decorated class, imported base), kept in the denominator.",
                         "Original 19-case corpus and historical evidence remain separate and unchanged."],
              "planned_cases": len(cases), "cases": []}
    report["case_manifest"] = [{"case": case["case"], "group": case["group"], "labels": case["labels"],
                                "patch_sha256": hashlib.sha256(patch_text(case["helper"], case["files"][case["helper"]], case["after"]).encode()).hexdigest()}
                               for case in cases]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.prepare_only:
        report["status"] = "prepared-only"
        report["cases"] = cases
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(f"Prepared {len(cases)} independently labelled cases; no Locus or model execution.")
        return
    plugin_repo = Path(__file__).resolve().parents[2]
    report["status"] = "running"
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="locus-resolution-corpus-") as scratch:
        root = Path(scratch)
        home = root / "home"
        install = home / "plugins/locus-codegraph"
        install.parent.mkdir(parents=True, exist_ok=True)
        if args.plugin_snapshot:
            shutil.copytree(args.plugin_snapshot, install)
        elif args.plugin_ref == "WORKTREE":
            filenames = git(plugin_repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
            for filename in filter(None, filenames):
                source = plugin_repo / filename
                if source.is_file():
                    dest = install / filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
        else:
            extract_commit(plugin_repo, args.plugin_ref, install)
        report.update({"plugin_ref": "frozen-snapshot" if args.plugin_snapshot else args.plugin_ref,
                       "plugin_base_commit": git_head(plugin_repo),
                       "plugin_engine_sha256": engine_digest(install)})
        (home / "config.yaml").write_text("plugins:\n  enabled: [locus-codegraph]\n  hook_callback_timeout: 120\n"
                                          "  entries:\n    locus-codegraph:\n      settings:\n        drift_guard: true\n"
                                          "        max_files: 2500\nterminal:\n  backend: local\n")
        path = os.environ.get("PATH", os.defpath)
        os.environ.clear()
        os.environ.update({"PATH": path, "HERMES_HOME": str(home), "HERMES_RUNTIME_DIR": str(args.runtime),
                           "TERMINAL_ENV": "local", "PYTHONDONTWRITEBYTECODE": "1"})
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(args.source))
        from agent.runtime_cwd import set_session_cwd
        from hermes_cli import lifecycle, plugins
        from model_tools import handle_function_call
        plugins.discover_plugins()
        active = next(row for row in plugins.get_plugin_manager().list_plugins() if row["name"] == "locus-codegraph")
        assert active["enabled"] and not active["error"], active
        previous = Path.cwd()
        try:
            for case in cases:
                workspace = root / case["case"]
                for filename, content in case["files"].items():
                    dest = workspace / filename
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)
                os.chdir(workspace)
                set_session_cwd(str(workspace))
                os.environ.update({"TERMINAL_CWD": str(workspace), "LOCUS_WORKSPACE": str(workspace)})
                lifecycle.invoke_hook("on_session_start", session_id=case["case"])
                payload = {"mode": "patch", "patch": patch_text(case["helper"], case["files"][case["helper"]], case["after"])}
                result = handle_function_call("patch", payload, session_id=case["case"], task_id=case["case"],
                                              turn_id="edit", api_request_id="edit", tool_call_id=case["case"])
                result = json.loads(result) if isinstance(result, str) else result
                assert result.get("success"), result
                assert (workspace / case["helper"]).read_text() == case["after"]
                warning = result.get("locus_drift_alert") or ""
                named = sorted(set(re.findall(r"Update caller `([^`]+:\d+)`", warning)))
                row = {key: value for key, value in case.items() if key not in {"files", "after"}}
                row.update({"patch_args": payload, "patch_result": result, "named_sites": named,
                            "files_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in case["files"].items()}})
                # Paths only; the run has no credential environment.
                encoded = json.dumps(row).replace(str(root), "<scratch>")
                report["cases"].append(json.loads(encoded))
                args.output.write_text(json.dumps(report, indent=2) + "\n")
                print(case["case"], "broken", sum(label["error"] is not None for label in case["labels"].values()),
                      "named", len(named), flush=True)
        finally:
            os.chdir(previous)
    report["summary"] = score(report["cases"])
    report["status"] = "complete"
    report["distinct_labelled_sites"] = len({(row.get("fixture_name", row["group"]), site)
                                             for row in report["cases"] for site in row["labels"]})
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    if args.check_cross_file:
        assert all(group["named_compatible"] == group["unlabelled_named"] == 0
                   and not any(site["scope"] == "cross-file" for site in group["missed_sites"])
                   for group in report["summary"].values()), report["summary"]


if __name__ == "__main__":
    main()
