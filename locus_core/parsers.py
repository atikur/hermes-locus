"""Multi-language structural code parsers (pure Python stdlib, zero external deps).

Provides:
- Deep AST extraction for Python (``.py``) via :mod:`ast`
- Fast comment-stripped structural extractors for TypeScript/JavaScript (``.ts``,
  ``.tsx``, ``.js``, ``.jsx``, ``.mjs``), Go (``.go``), and Rust (``.rs``)
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, TypedDict

LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
}


class PythonSignature(TypedDict):
    posonly: list[str]
    positional_or_keyword: list[str]
    keyword_only: list[str]
    required: list[str]
    has_var_positional: bool
    has_var_keyword: bool


class _PythonBindingMetadata(TypedDict, total=False):
    binding_version: int
    import_module: str
    import_name: str
    import_kind: str
    attribute_path: list[str]
    callee_expression: list[str]


class PythonCall(_PythonBindingMetadata):
    positional_count: int
    keywords: list[str]
    has_star_args: bool
    has_star_kwargs: bool
    direct_import: bool


@dataclass(slots=True)
class ParsedSymbol:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str  # function | async_function | class | method | interface | type | struct | trait | constant
    rel_path: str
    line_start: int
    line_end: int
    signature: str
    min_args: int = 0
    max_args: int = -1
    is_exported: bool = True
    docstring: str = ""
    python_signature: PythonSignature | None = None


@dataclass(slots=True)
class ParsedImport:
    src_path: str
    target_spec: str  # e.g. ".utils", "locus_core.graph", "./api"
    imported_names: list[str] = field(default_factory=list)
    line_no: int = 1


@dataclass(slots=True)
class ParsedCall:
    caller_path: str
    caller_symbol: str
    callee_name: str
    line_no: int
    arg_count: int = -1  # -1 means variadic/unpacked or unknown
    python_call: PythonCall | None = None


@dataclass(slots=True)
class ParsedFile:
    rel_path: str
    lang: str
    line_count: int
    is_test: bool
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)
    parse_error: str = ""
    python_module: dict[str, Any] | None = None


def detect_language(rel_path: str) -> Optional[str]:
    ext = os.path.splitext(rel_path)[1].lower()
    return LANGUAGE_BY_EXT.get(ext)


def is_test_file(rel_path: str) -> bool:
    norm = rel_path.replace("\\", "/")
    parts = norm.lower().split("/")
    base = parts[-1]
    if any(p in {"tests", "test", "__tests__", "spec", "specs"} for p in parts[:-1]):
        return True
    if base.startswith("test_") or base.endswith(("_test.py", "_test.go", "_test.rs")):
        return True
    if any(tok in base for tok in (".test.", ".spec.")):
        return True
    return False


def _first_doc_line(doc: Optional[str]) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        clean = line.strip()
        if clean:
            return clean[:200]
    return ""


# ─── Python AST Parser ───────────────────────────────────────────────────────


def python_call_compatibility(
    signature: PythonSignature | None, call: PythonCall | None,
) -> tuple[bool | None, str]:
    """Bind explicit arguments to a static signature; leave unpacked calls uncertain."""
    if signature is None or call is None:
        return None, "Python call shape is unavailable"
    if call["has_star_args"] or call["has_star_kwargs"]:
        return None, "argument unpacking may change the call shape"

    positional_only = signature["posonly"]
    positional_names = positional_only + signature["positional_or_keyword"]
    keyword_only = set(signature["keyword_only"])
    keyword_names = keyword_only.union(signature["positional_or_keyword"])
    count = call["positional_count"]
    if count > len(positional_names) and not signature["has_var_positional"]:
        return False, f"passes {count} positional arguments but accepts at most {len(positional_names)}"

    bound = set(positional_names[:count])
    seen_keywords: set[str] = set()
    for name in call["keywords"]:
        if name in seen_keywords:
            return False, f"duplicate keyword argument {name!r}"
        seen_keywords.add(name)
        if name in keyword_names:
            if name in bound:
                return False, f"multiple values for argument {name!r}"
            bound.add(name)
        elif not signature["has_var_keyword"]:
            if name in positional_only:
                return False, f"positional-only argument {name!r} passed by keyword"
            return False, f"unexpected keyword argument {name!r}"

    for name in signature["required"]:
        if name not in bound:
            if name in keyword_only:
                return False, f"missing required keyword-only argument {name!r}"
            if name in positional_only:
                return False, f"missing required positional-only argument {name!r}"
            return False, f"missing required argument {name!r}"
    return True, ""


def _python_signature(args: ast.arguments) -> PythonSignature:
    posonly = [arg.arg for arg in args.posonlyargs]
    positional_or_keyword = [arg.arg for arg in args.args]
    positional = posonly + positional_or_keyword
    required_positional = len(positional) - len(args.defaults)
    return {
        "posonly": posonly,
        "positional_or_keyword": positional_or_keyword,
        "keyword_only": [arg.arg for arg in args.kwonlyargs],
        "required": positional[:required_positional] + [
            arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults) if default is None
        ],
        "has_var_positional": args.vararg is not None,
        "has_var_keyword": args.kwarg is not None,
    }


def _unparse_safe(node: Optional[ast.AST]) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return "..."


def _python_func_signature_and_arity(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    is_method: bool = False,
) -> tuple[str, int, int]:
    """Build clean signature string and (min_args, max_args) for a Python function/method."""
    decorators = {_unparse_safe(d).split("(")[0].split(".")[-1] for d in node.decorator_list}
    drop_first_receiver = is_method and "staticmethod" not in decorators

    posonly = list(node.args.posonlyargs)
    regular = list(node.args.args)
    all_pos = posonly + regular
    if drop_first_receiver and all_pos:
        all_pos_for_count = all_pos[1:]
    else:
        all_pos_for_count = all_pos

    num_defaults = len(node.args.defaults)
    required_pos = max(0, len(all_pos_for_count) - num_defaults)

    required_kwonly = sum(
        1 for kw, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None
    )
    min_args = required_pos + required_kwonly

    has_variadic = node.args.vararg is not None or node.args.kwarg is not None
    max_args = -1 if has_variadic else (len(all_pos_for_count) + len(node.args.kwonlyargs))

    # Build display parameter list
    rendered_params: list[str] = []
    default_offset = len(all_pos) - len(node.args.defaults)
    for idx, arg in enumerate(all_pos):
        piece = arg.arg
        if arg.annotation is not None:
            piece += f": {_unparse_safe(arg.annotation)}"
        if idx >= default_offset:
            d_node = node.args.defaults[idx - default_offset]
            d_str = _unparse_safe(d_node)
            if len(d_str) > 24:
                d_str = "..."
            piece += f" = {d_str}"
        rendered_params.append(piece)
        if idx + 1 == len(posonly):
            rendered_params.append("/")

    if node.args.vararg is not None:
        va = f"*{node.args.vararg.arg}"
        if node.args.vararg.annotation is not None:
            va += f": {_unparse_safe(node.args.vararg.annotation)}"
        rendered_params.append(va)
    elif node.args.kwonlyargs:
        rendered_params.append("*")

    for kw, kw_def in zip(node.args.kwonlyargs, node.args.kw_defaults):
        piece = kw.arg
        if kw.annotation is not None:
            piece += f": {_unparse_safe(kw.annotation)}"
        if kw_def is not None:
            d_str = _unparse_safe(kw_def)
            if len(d_str) > 24:
                d_str = "..."
            piece += f" = {d_str}"
        rendered_params.append(piece)

    if node.args.kwarg is not None:
        kwa = f"**{node.args.kwarg.arg}"
        if node.args.kwarg.annotation is not None:
            kwa += f": {_unparse_safe(node.args.kwarg.annotation)}"
        rendered_params.append(kwa)

    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    ret = f" -> {_unparse_safe(node.returns)}" if node.returns is not None else ""
    sig = f"{prefix} {node.name}({', '.join(rendered_params)}){ret}"
    return sig, min_args, max_args


_PythonFunction = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass
class _PythonBinding:
    kind: str
    module: str = ""
    name: str = ""
    line: int = 0
    unconditional: bool = False


@dataclass
class _PythonScope:
    node: ast.AST
    parent: "_PythonScope | None"
    kind: str
    name: str
    bindings: dict[str, list[_PythonBinding]] = field(default_factory=dict)
    star_import: bool = False
    dynamic: bool = False

    def bind(self, name: str, binding: _PythonBinding | None = None) -> None:
        self.bindings.setdefault(name, []).append(binding or _PythonBinding("unknown"))


def _attribute_parts(node: ast.AST) -> list[str]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return []
    return [node.id, *reversed(parts)]


class _PythonCallCollector(ast.NodeVisitor):
    """Collect lexical import bindings, declining ambiguous/dynamic targets.

    This is intentionally not dataflow inference: assignments, conditional imports,
    star imports and dynamic mutation prevent a confident binding. A shadow in one
    function does not poison an independent function's local import.
    """

    def __init__(self, rel_path: str, tree: ast.Module) -> None:
        self.rel_path = rel_path
        self.root = self.scope = _PythonScope(tree, None, "module", "<module>")
        self.calls: list[ParsedCall] = []
        self.imports: list[ParsedImport] = []
        self.pending: list[tuple[ParsedCall, list[str], _PythonScope]] = []
        self.nonlocal_mutations: set[str] = set()
        self.attribute_mutations: set[str] = set()

    def _unconditional(self, node: ast.AST) -> bool:
        return node in getattr(self.scope.node, "body", ())

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.scope.bind(bound, _PythonBinding(
                "module", alias.name, bound if alias.asname else "", node.lineno,
                self._unconditional(node),
            ))
            self.imports.append(ParsedImport(self.rel_path, alias.name, [], node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        spec = "." * node.level + (node.module or "")
        for alias in node.names:
            if alias.name == "*":
                self.scope.star_import = True
                continue
            self.scope.bind(alias.asname or alias.name, _PythonBinding(
                "from", spec, alias.name, node.lineno, self._unconditional(node),
            ))
            self.imports.append(ParsedImport(self.rel_path, spec, [alias.name], node.lineno))

    def _child_scope(self, node: ast.AST, kind: str, name: str) -> _PythonScope:
        parent = self.scope
        qname = name if parent.name == "<module>" else f"{parent.name}.{name}"
        return _PythonScope(node, parent, kind, qname)

    def _arguments(self, args: ast.arguments) -> None:
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                self.scope.bind(arg.arg)

    def _visit_fn(self, node: _PythonFunction) -> None:
        self.scope.bind(node.name, _PythonBinding(
            "function" if self._unconditional(node) and not node.decorator_list else "unknown",
            line=node.lineno, unconditional=self._unconditional(node),
        ))
        # Defaults and decorators execute in the enclosing scope, not the body.
        for item in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)
        previous = self.scope
        self.scope = self._child_scope(node, "function", node.name)
        self._arguments(node.args)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = previous

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.bind(node.name)
        for item in [*node.bases, *node.decorator_list, *node.keywords]:
            self.visit(item)
        previous = self.scope
        self.scope = self._child_scope(node, "class", node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.scope = previous

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for item in [*node.args.defaults, *node.args.kw_defaults]:
            if item is not None:
                self.visit(item)
        previous = self.scope
        self.scope = self._child_scope(node, "function", "<lambda>")
        self._arguments(node.args)
        self.visit(node.body)
        self.scope = previous

    def _comprehension(self, node: ast.AST) -> None:
        generators = node.generators
        self.visit(generators[0].iter)
        previous = self.scope
        self.scope = self._child_scope(node, "function", "<comprehension>")
        for i, gen in enumerate(generators):
            if i:
                self.visit(gen.iter)
            self.visit(gen.target)
            for condition in gen.ifs:
                self.visit(condition)
        for name in ("elt", "key", "value"):
            item = getattr(node, name, None)
            if item is not None:
                self.visit(item)
        self.scope = previous

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bind(node.id)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        scope = self.scope
        while isinstance(scope.node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            assert scope.parent is not None
            scope = scope.parent
        if isinstance(node.target, ast.Name):
            scope.bind(node.target.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            parts = _attribute_parts(node)
            if parts:
                self.attribute_mutations.add(parts[0])
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        # Reads with global/nonlocal declarations could be supported later; mutations
        # can affect calls in other scopes and require dataflow to reason about.
        self.nonlocal_mutations.update(node.names)

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.scope.bind(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.scope.bind(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.scope.bind(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.scope.bind(node.rest)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        parts = _attribute_parts(node.func)
        if parts and parts[0] in {"exec", "eval", "globals", "locals", "__import__"}:
            self.scope.dynamic = True
        if parts and parts[0] in {"setattr", "delattr"} and node.args:
            target = _attribute_parts(node.args[0])
            if target:
                self.attribute_mutations.add(target[0])
        if parts and not (len(parts) == 1 and parts[0] in {
            "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "print",
            "isinstance", "getattr", "setattr", "hasattr", "range", "enumerate", "sorted", "min",
            "max", "sum", "any", "all", "zip", "super", "open",
        }):
            positional = sum(not isinstance(arg, ast.Starred) for arg in node.args)
            keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
            star_args, star_kwargs = positional != len(node.args), len(keywords) != len(node.keywords)
            shape: PythonCall = {
                "positional_count": positional, "keywords": keywords,
                "has_star_args": star_args, "has_star_kwargs": star_kwargs,
                "direct_import": False, "binding_version": 2,
                "callee_expression": parts,
            }
            call = ParsedCall(
                self.rel_path, self.scope.name, parts[-1], node.lineno,
                -1 if star_args or star_kwargs else positional + len(keywords), shape,
            )
            self.calls.append(call)
            self.pending.append((call, parts, self.scope))
        self.generic_visit(node)

    def _lookup(self, name: str, scope: _PythonScope, line: int) -> _PythonBinding | None:
        if name in self.nonlocal_mutations or name in self.attribute_mutations:
            return None
        current = scope
        while current is not None:
            if current.star_import or current.dynamic:
                return None
            entries = current.bindings.get(name)
            if entries:
                if len(entries) != 1:
                    return None
                binding = entries[0]
                if not binding.unconditional or binding.kind not in {"module", "from"}:
                    return None
                # Conservatively require lexical declaration before the use even
                # across closures (module execution order is otherwise unknown).
                return binding if binding.line < line else None
            parent = current.parent
            if current.kind == "function":
                while parent is not None and parent.kind == "class":
                    parent = parent.parent
            current = parent
        return None

    def mark_direct_imports(self) -> None:
        for call, parts, scope in self.pending:
            binding = self._lookup(parts[0], scope, call.line_no)
            if binding is None:
                continue
            attrs = parts[1:]
            if binding.kind == "module":
                if binding.name:  # import package.module as alias
                    suffix = attrs
                else:  # import package.module binds the package root
                    prefix = binding.module.split(".")[1:]
                    if attrs[:len(prefix)] != prefix:
                        continue
                    suffix = attrs[len(prefix):]
                if len(suffix) != 1:
                    continue
                module, imported_name = binding.module, ""
            else:
                # A direct function or a member of an explicitly imported module.
                if len(attrs) > 1:
                    continue
                module, imported_name, suffix = binding.module, binding.name, attrs
            assert call.python_call is not None
            call.python_call.update({
                "direct_import": True, "import_module": module,
                "import_name": imported_name, "import_kind": binding.kind,
                "attribute_path": suffix,
            })
            call.callee_name = suffix[-1] if suffix else imported_name

    def module_metadata(self) -> dict[str, Any]:
        bindings = {}
        imports = {}
        for name, entries in self.root.bindings.items():
            entry = entries[0]
            bindings[name] = entry.kind if (len(entries) == 1 and entry.unconditional
                and name not in self.nonlocal_mutations and name not in self.attribute_mutations) else "unknown"
            if bindings[name] in {"from", "module"}:
                imports[name] = {"kind": entry.kind, "module": entry.module, "name": entry.name}
        return {"bindings": bindings, "imports": imports, "dynamic": self.root.dynamic or self.root.star_import}


def _parse_python(rel_path: str, source: str) -> ParsedFile:
    lines = source.splitlines()
    parsed = ParsedFile(
        rel_path=rel_path,
        lang="python",
        line_count=len(lines),
        is_test=is_test_file(rel_path),
    )
    try:
        tree = ast.parse(source, filename=rel_path)
    except SyntaxError as exc:
        # An incomplete edit is not an empty module. Keep this explicit so the
        # indexer can retain the last valid graph rather than infer deletions.
        parsed.parse_error = f"{exc.msg} (line {exc.lineno or 1})"
        return parsed

    explicit_all: Optional[set[str]] = None
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(stmt.value, (ast.List, ast.Tuple, ast.Set)):
                        explicit_all = {
                            elt.value
                            for elt in stmt.value.elts
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                        }

    def _exported(name: str) -> bool:
        if explicit_all is not None:
            return name in explicit_all
        return not name.startswith("_")

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sig, min_a, max_a = _python_func_signature_and_arity(stmt, is_method=False)
            kind = "async_function" if isinstance(stmt, ast.AsyncFunctionDef) else "function"
            parsed.symbols.append(
                ParsedSymbol(
                    symbol_id=f"{rel_path}::{stmt.name}",
                    name=stmt.name,
                    qualified_name=stmt.name,
                    kind=kind,
                    rel_path=rel_path,
                    line_start=stmt.lineno,
                    line_end=getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno,
                    signature=sig,
                    min_args=min_a,
                    max_args=max_a,
                    is_exported=_exported(stmt.name),
                    docstring=_first_doc_line(ast.get_docstring(stmt)),
                    # A decorator may replace the declared binding rules.
                    python_signature=_python_signature(stmt.args) if not stmt.decorator_list else None,
                )
            )
        elif isinstance(stmt, ast.ClassDef):
            bases = [_unparse_safe(b) for b in stmt.bases]
            base_part = f"({', '.join(bases)})" if bases else ""
            init_min, init_max = 0, -1
            for body_item in stmt.body:
                if isinstance(body_item, ast.FunctionDef) and body_item.name == "__init__":
                    _, init_min, init_max = _python_func_signature_and_arity(body_item, is_method=True)
                    break
            parsed.symbols.append(
                ParsedSymbol(
                    symbol_id=f"{rel_path}::{stmt.name}",
                    name=stmt.name,
                    qualified_name=stmt.name,
                    kind="class",
                    rel_path=rel_path,
                    line_start=stmt.lineno,
                    line_end=getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno,
                    signature=f"class {stmt.name}{base_part}",
                    min_args=init_min,
                    max_args=init_max,
                    is_exported=_exported(stmt.name),
                    docstring=_first_doc_line(ast.get_docstring(stmt)),
                )
            )
            for body_item in stmt.body:
                if isinstance(body_item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if body_item.name.startswith("__") and body_item.name != "__init__":
                        continue
                    msig, mmin, mmax = _python_func_signature_and_arity(body_item, is_method=True)
                    qname = f"{stmt.name}.{body_item.name}"
                    parsed.symbols.append(
                        ParsedSymbol(
                            symbol_id=f"{rel_path}::{qname}",
                            name=body_item.name,
                            qualified_name=qname,
                            kind="method",
                            rel_path=rel_path,
                            line_start=body_item.lineno,
                            line_end=getattr(body_item, "end_lineno", body_item.lineno) or body_item.lineno,
                            signature=msig,
                            min_args=mmin,
                            max_args=mmax,
                            is_exported=_exported(stmt.name) and not body_item.name.startswith("_"),
                            docstring=_first_doc_line(ast.get_docstring(body_item)),
                        )
                    )
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id.isupper() and len(target.id) >= 3:
                    parsed.symbols.append(
                        ParsedSymbol(
                            symbol_id=f"{rel_path}::{target.id}",
                            name=target.id,
                            qualified_name=target.id,
                            kind="constant",
                            rel_path=rel_path,
                            line_start=stmt.lineno,
                            line_end=getattr(stmt, "end_lineno", stmt.lineno) or stmt.lineno,
                            signature=f"{target.id} = ...",
                            min_args=0,
                            max_args=0,
                            is_exported=_exported(target.id),
                        )
                    )

    collector = _PythonCallCollector(rel_path, tree)
    collector.visit(tree)
    collector.mark_direct_imports()
    parsed.imports = collector.imports
    parsed.calls = collector.calls
    parsed.python_module = collector.module_metadata()
    return parsed


# ─── C-Style Comment Stripper (preserves line numbers) ───────────────────────

_C_COMMENTS_RE = re.compile(
    r'("(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`)'
    r"|/\*[\s\S]*?\*/|//[^\n]*"
)


def _strip_c_comments(source: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        if m.group(1) is not None:
            return m.group(1)
        return "\n" * m.group(0).count("\n")

    return _C_COMMENTS_RE.sub(_repl, source)


def _count_params_c_style(param_text: str) -> tuple[int, int]:
    cleaned = param_text.strip()
    if not cleaned:
        return 0, 0
    # Split top-level commas ignoring nested (), <>, {}, []
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in cleaned:
        if ch in "(<{[":
            depth += 1
            cur.append(ch)
        elif ch in ")>}]":
            depth = max(0, depth - 1)
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        tail = "".join(cur).strip()
        if tail:
            parts.append(tail)

    min_a = 0
    variadic = False
    for p in parts:
        if p.startswith("...") or "..." in p:
            variadic = True
            continue
        if "=" in p or "?" in p.split(":")[0]:
            continue
        min_a += 1
    max_a = -1 if variadic else len(parts)
    return min_a, max_a


# ─── TypeScript / JavaScript Parser ──────────────────────────────────────────

_TS_IMPORT_RE = re.compile(
    r"""(?m)^\s*import\s+(?:type\s+)?(?:(?P<clause>[\s\S]*?)\s+from\s+)?['"](?P<spec>[^'"]+)['"]"""
)
_TS_EXPORT_FROM_RE = re.compile(
    r"""(?m)^\s*export\s+(?:\*|\{[^}]*\})\s+from\s+['"](?P<spec>[^'"]+)['"]"""
)
_TS_REQUIRE_RE = re.compile(r"""\brequire\(\s*['"](?P<spec>[^'"]+)['"]\s*\)""")
_TS_FUNC_RE = re.compile(
    r"""(?m)^[ \t]*(?P<exp>export\s+(?:default\s+)?)?(?P<async>async\s+)?function\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\((?P<params>[^)]*)\)"""
)
_TS_ARROW_RE = re.compile(
    r"""(?m)^[ \t]*(?P<exp>export\s+)?(?:const|let)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?P<async>async\s+)?(?:\((?P<params>[^)]*)\)|(?P<single>[A-Za-z_$][\w$]*))\s*=>"""
)
_TS_TYPE_DECL_RE = re.compile(
    r"""(?m)^[ \t]*(?P<exp>export\s+(?:default\s+)?)?(?:abstract\s+)?(?P<kind>class|interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)"""
)
_CALL_IDENT_RE = re.compile(r"""(?<![.\w$])(?:[A-Za-z_$][\w$]*\.)?(?P<name>[A-Za-z_$][\w$]*)\s*\(""")
_JS_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "function", "typeof",
    "import", "require", "new", "super", "console", "log", "warn", "error",
    "Promise", "Array", "Object", "String", "Number", "Boolean", "Math", "JSON",
    "parseInt", "parseFloat", "setTimeout", "clearTimeout", "setInterval", "Map", "Set",
}


def _line_of_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _parse_ts_js(rel_path: str, source: str, lang: str) -> ParsedFile:
    stripped = _strip_c_comments(source)
    lines = source.splitlines()
    parsed = ParsedFile(
        rel_path=rel_path,
        lang=lang,
        line_count=len(lines),
        is_test=is_test_file(rel_path),
    )

    for m in _TS_IMPORT_RE.finditer(stripped):
        clause = (m.group("clause") or "").strip()
        names = [
            tok.strip().split(" as ")[0].strip()
            for tok in clause.replace("{", "").replace("}", "").split(",")
            if tok.strip() and not tok.strip().startswith("*")
        ]
        parsed.imports.append(
            ParsedImport(
                src_path=rel_path,
                target_spec=m.group("spec"),
                imported_names=names,
                line_no=_line_of_offset(stripped, m.start()),
            )
        )
    for m in _TS_EXPORT_FROM_RE.finditer(stripped):
        parsed.imports.append(
            ParsedImport(
                src_path=rel_path,
                target_spec=m.group("spec"),
                imported_names=[],
                line_no=_line_of_offset(stripped, m.start()),
            )
        )
    for m in _TS_REQUIRE_RE.finditer(stripped):
        parsed.imports.append(
            ParsedImport(
                src_path=rel_path,
                target_spec=m.group("spec"),
                imported_names=[],
                line_no=_line_of_offset(stripped, m.start()),
            )
        )

    seen_ids: set[str] = set()
    for m in _TS_FUNC_RE.finditer(stripped):
        name = m.group("name")
        sid = f"{rel_path}::{name}"
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        lineno = _line_of_offset(stripped, m.start())
        params = " ".join(m.group("params").split())
        min_a, max_a = _count_params_c_style(params)
        is_async = bool(m.group("async"))
        prefix = "async function" if is_async else "function"
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=sid,
                name=name,
                qualified_name=name,
                kind="async_function" if is_async else "function",
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 15),
                signature=f"{prefix} {name}({params})",
                min_args=min_a,
                max_args=max_a,
                is_exported=bool(m.group("exp")),
            )
        )

    for m in _TS_ARROW_RE.finditer(stripped):
        name = m.group("name")
        sid = f"{rel_path}::{name}"
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        lineno = _line_of_offset(stripped, m.start())
        raw_params = m.group("params") if m.group("params") is not None else (m.group("single") or "")
        params = " ".join(raw_params.split())
        min_a, max_a = _count_params_c_style(params)
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=sid,
                name=name,
                qualified_name=name,
                kind="function",
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 12),
                signature=f"const {name} = ({params}) => ...",
                min_args=min_a,
                max_args=max_a,
                is_exported=bool(m.group("exp")),
            )
        )

    for m in _TS_TYPE_DECL_RE.finditer(stripped):
        name = m.group("name")
        kind = m.group("kind")
        sid = f"{rel_path}::{name}"
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        lineno = _line_of_offset(stripped, m.start())
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=sid,
                name=name,
                qualified_name=name,
                kind=kind,
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 20),
                signature=f"{kind} {name}",
                min_args=0,
                max_args=-1,
                is_exported=bool(m.group("exp")),
            )
        )

    defined_names = {s.name for s in parsed.symbols}
    for idx, line in enumerate(stripped.splitlines(), start=1):
        for cm in _CALL_IDENT_RE.finditer(line):
            cname = cm.group("name")
            if cname in _JS_KEYWORDS:
                continue
            # Skip obvious declaration line itself
            if cname in defined_names and re.search(rf"\bfunction\s+{re.escape(cname)}\b", line):
                continue
            parsed.calls.append(
                ParsedCall(
                    caller_path=rel_path,
                    caller_symbol="<module>",
                    callee_name=cname,
                    line_no=idx,
                    arg_count=-1,
                )
            )
    return parsed


# ─── Go & Rust Structural Parsers ────────────────────────────────────────────

_GO_IMPORT_SINGLE_RE = re.compile(r'(?m)^\s*import\s+(?:[\w.]+\s+)?"(?P<spec>[^"]+)"')
_GO_IMPORT_BLOCK_RE = re.compile(r"(?s)\bimport\s*\((?P<body>.*?)\)")
_GO_FUNC_RE = re.compile(
    r"(?m)^func\s+(?:\(\s*\w+\s+\*?(?P<recv>[A-Za-z_]\w*)\s*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^)]*)\)"
)
_GO_TYPE_RE = re.compile(r"(?m)^type\s+(?P<name>[A-Za-z_]\w*)\s+(?P<kind>struct|interface)\b")

_RS_USE_RE = re.compile(r"(?m)^\s*(?:pub\s+)?use\s+(?P<spec>[A-Za-z0-9_:{}*,\s]+);")
_RS_MOD_RE = re.compile(r"(?m)^\s*(?:pub\s+)?mod\s+(?P<name>[A-Za-z_]\w*)\s*;")
_RS_FN_RE = re.compile(
    r"(?m)^[ \t]*(?P<pub>pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(?P<name>[A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\((?P<params>[^)]*)\)"
)
_RS_TYPE_RE = re.compile(
    r"(?m)^[ \t]*(?P<pub>pub(?:\([^)]*\))?\s+)?(?P<kind>struct|enum|trait)\s+(?P<name>[A-Za-z_]\w*)"
)


def _parse_go(rel_path: str, source: str) -> ParsedFile:
    stripped = _strip_c_comments(source)
    lines = source.splitlines()
    parsed = ParsedFile(rel_path=rel_path, lang="go", line_count=len(lines), is_test=is_test_file(rel_path))
    for m in _GO_IMPORT_SINGLE_RE.finditer(stripped):
        parsed.imports.append(
            ParsedImport(src_path=rel_path, target_spec=m.group("spec"), line_no=_line_of_offset(stripped, m.start()))
        )
    for bm in _GO_IMPORT_BLOCK_RE.finditer(stripped):
        base_line = _line_of_offset(stripped, bm.start())
        for offset_idx, line in enumerate(bm.group("body").splitlines()):
            sm = re.search(r'"([^"]+)"', line)
            if sm:
                parsed.imports.append(
                    ParsedImport(src_path=rel_path, target_spec=sm.group(1), line_no=base_line + offset_idx)
                )
    for m in _GO_FUNC_RE.finditer(stripped):
        recv = m.group("recv")
        name = m.group("name")
        qname = f"{recv}.{name}" if recv else name
        params = " ".join(m.group("params").split())
        min_a, max_a = _count_params_c_style(params)
        lineno = _line_of_offset(stripped, m.start())
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=f"{rel_path}::{qname}",
                name=name,
                qualified_name=qname,
                kind="method" if recv else "function",
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 15),
                signature=f"func {qname}({params})",
                min_args=min_a,
                max_args=max_a,
                is_exported=name[:1].isupper(),
            )
        )
    for m in _GO_TYPE_RE.finditer(stripped):
        name = m.group("name")
        kind = m.group("kind")
        lineno = _line_of_offset(stripped, m.start())
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=f"{rel_path}::{name}",
                name=name,
                qualified_name=name,
                kind=kind,
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 15),
                signature=f"type {name} {kind}",
                is_exported=name[:1].isupper(),
            )
        )
    return parsed


def _parse_rust(rel_path: str, source: str) -> ParsedFile:
    stripped = _strip_c_comments(source)
    lines = source.splitlines()
    parsed = ParsedFile(
        rel_path=rel_path,
        lang="rust",
        line_count=len(lines),
        is_test=is_test_file(rel_path) or "#[cfg(test)]" in stripped,
    )
    for m in _RS_MOD_RE.finditer(stripped):
        parsed.imports.append(
            ParsedImport(src_path=rel_path, target_spec=m.group("name"), line_no=_line_of_offset(stripped, m.start()))
        )
    for m in _RS_USE_RE.finditer(stripped):
        spec = " ".join(m.group("spec").split())
        parsed.imports.append(
            ParsedImport(src_path=rel_path, target_spec=spec, line_no=_line_of_offset(stripped, m.start()))
        )
    for m in _RS_FN_RE.finditer(stripped):
        name = m.group("name")
        raw_params = " ".join(m.group("params").split())
        # Ignore leading &self / &mut self / self
        param_items = [
            p.strip()
            for p in raw_params.split(",")
            if p.strip() and p.strip() not in {"self", "&self", "&mut self", "mut self"}
        ]
        min_a = len(param_items)
        lineno = _line_of_offset(stripped, m.start())
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=f"{rel_path}::{name}",
                name=name,
                qualified_name=name,
                kind="function",
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 15),
                signature=f"fn {name}({raw_params})",
                min_args=min_a,
                max_args=min_a,
                is_exported=bool(m.group("pub")),
            )
        )
    for m in _RS_TYPE_RE.finditer(stripped):
        name = m.group("name")
        kind = m.group("kind")
        lineno = _line_of_offset(stripped, m.start())
        parsed.symbols.append(
            ParsedSymbol(
                symbol_id=f"{rel_path}::{name}",
                name=name,
                qualified_name=name,
                kind=kind,
                rel_path=rel_path,
                line_start=lineno,
                line_end=min(len(lines), lineno + 15),
                signature=f"{kind} {name}",
                is_exported=bool(m.group("pub")),
            )
        )
    return parsed


def parse_source_file(rel_path: str, source: str) -> Optional[ParsedFile]:
    """Dispatch *rel_path* to the appropriate language parser."""
    lang = detect_language(rel_path)
    if lang is None:
        return None
    if lang == "python":
        return _parse_python(rel_path, source)
    if lang in ("typescript", "javascript"):
        return _parse_ts_js(rel_path, source, lang)
    if lang == "go":
        return _parse_go(rel_path, source)
    if lang == "rust":
        return _parse_rust(rel_path, source)
    return None
