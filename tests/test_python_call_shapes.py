"""Static Python call binding and confidence boundaries."""

from __future__ import annotations

import unittest

from locus_core.parsers import parse_source_file, python_call_compatibility


class TestPythonCallShapes(unittest.TestCase):
    def test_keyword_rename_distinguishes_positional_and_keyword_callers(self) -> None:
        old = parse_source_file("library.py", "def prepare(max_len: int): pass")
        new = parse_source_file("library.py", "def prepare(limit: int): pass")
        callers = parse_source_file(
            "consumer.py", "from library import prepare\nprepare(40)\nprepare(max_len=40)"
        )
        assert old is not None and new is not None and callers is not None
        old_signature, new_signature = old.symbols[0].python_signature, new.symbols[0].python_signature
        self.assertEqual([call.arg_count for call in callers.calls], [1, 1])
        self.assertEqual(python_call_compatibility(new_signature, callers.calls[0].python_call), (True, ""))
        self.assertEqual(python_call_compatibility(old_signature, callers.calls[1].python_call), (True, ""))
        self.assertEqual(
            python_call_compatibility(new_signature, callers.calls[1].python_call),
            (False, "unexpected keyword argument 'max_len'"),
        )

    def test_parameter_kinds_and_defaults(self) -> None:
        module = parse_source_file("library.py", "async def prepare(path, /, mode='r', *, limit): pass")
        assert module is not None
        signature = module.symbols[0]
        self.assertIn("path, /, mode = 'r', *, limit", signature.signature)
        cases = {
            "prepare('example', limit=5)": (True, ""),
            "prepare('example', mode='w', limit=5)": (True, ""),
            "prepare(path='example', limit=5)": (False, "positional-only argument 'path' passed by keyword"),
            "prepare('example', 'w', 5)": (False, "passes 3 positional arguments but accepts at most 2"),
            "prepare('example', 'w', mode='r', limit=5)": (False, "multiple values for argument 'mode'"),
            "prepare('example')": (False, "missing required keyword-only argument 'limit'"),
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                callers = parse_source_file("consumer.py", expression)
                assert callers is not None
                self.assertEqual(
                    python_call_compatibility(signature.python_signature, callers.calls[0].python_call), expected
                )

    def test_variadics_bind_explicit_arguments_but_unpacking_is_unknown(self) -> None:
        module = parse_source_file("library.py", "def prepare(path='.', /, *extras, **options): pass")
        callers = parse_source_file(
            "consumer.py", "prepare(path='keyword')\nprepare('.', 1, 2)\nprepare(*paths)\nprepare(**options)"
        )
        assert module is not None and callers is not None
        signature = module.symbols[0].python_signature
        self.assertEqual([call.arg_count for call in callers.calls], [1, 3, -1, -1])
        self.assertEqual(
            [python_call_compatibility(signature, call.python_call)[0] for call in callers.calls],
            [True, True, None, None],
        )

    def test_decorated_function_keeps_arity_fallback(self) -> None:
        module = parse_source_file("library.py", "@wrapper\ndef prepare(limit): pass")
        assert module is not None
        self.assertIsNone(module.symbols[0].python_signature)
        self.assertEqual((module.symbols[0].min_args, module.symbols[0].max_args), (1, 1))

    def test_direct_imports_are_limited_to_provable_scope_and_order(self) -> None:
        module = parse_source_file("consumer.py", """
from library import outer
from library import shadowed
from library import rebound
from library import aliased
from other import aliased as another_name
if condition:
    from library import conditional

def consume(shadowed):
    outer(old=1)
    shadowed(old=1)
    rebound = shadowed
    rebound(old=1)
    aliased(old=1)
    conditional(old=1)
    local(old=1)
    from library import local
    local(old=1)
    other.outer(old=1)

def other_scope():
    local(old=1)
""")
        assert module is not None
        self.assertEqual(
            [(call.callee_name, call.python_call and call.python_call["direct_import"]) for call in module.calls],
            [
                ("outer", True), ("shadowed", False), ("rebound", False),
                ("aliased", False), ("conditional", False),
                ("local", False), ("local", True), ("outer", None), ("local", False),
            ],
        )
