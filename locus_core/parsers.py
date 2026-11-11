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
from typing import Iterable, Optional

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


@dataclass(slots=True)
class ParsedFile:
    rel_path: str
    lang: str
    line_count: int
    is_test: bool
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)


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


class _PythonCallCollector(ast.NodeVisitor):
    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.scope_stack: list[str] = ["<module>"]
        self.calls: list[ParsedCall] = []
        self.imports: list[ParsedImport] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ParsedImport(
                    src_path=self.rel_path,
                    target_spec=alias.name,
                    imported_names=[alias.asname or alias.name.split(".")[-1]],
                    line_no=getattr(node, "lineno", 1),
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        dots = "." * (node.level or 0)
        mod = node.module or ""
        spec = f"{dots}{mod}" if (dots or mod) else "."
        names = [a.name for a in node.names if a.name != "*"]
        self.imports.append(
            ParsedImport(
                src_path=self.rel_path,
                target_spec=spec,
                imported_names=names,
                line_no=getattr(node, "lineno", 1),
            )
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_fn(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_fn(node)

    def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.scope_stack[-1]
        qname = f"{parent}.{node.name}" if parent != "<module>" else node.name
        self.scope_stack.append(qname)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        callee = ""
        if isinstance(node.func, ast.Name):
            callee = node.func.id
        elif isinstance(node.func, ast.Attribute):
            callee = node.func.attr

        if callee and callee not in {
            "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
            "print", "isinstance", "getattr", "setattr", "hasattr", "range", "enumerate",
            "sorted", "min", "max", "sum", "any", "all", "zip", "super", "open",
            "append", "extend", "get", "items", "keys", "values", "strip", "split", "join",
            "format", "lower", "upper", "replace", "startswith", "endswith",
        }:
            has_unpack = any(isinstance(a, ast.Starred) for a in node.args) or any(
                kw.arg is None for kw in node.keywords
            )
            arg_count = -1 if has_unpack else (len(node.args) + len(node.keywords))
            self.calls.append(
                ParsedCall(
                    caller_path=self.rel_path,
                    caller_symbol=self.scope_stack[-1],
                    callee_name=callee,
                    line_no=getattr(node, "lineno", 1),
                    arg_count=arg_count,
                )
            )
        self.generic_visit(node)


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
    except SyntaxError:
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

    collector = _PythonCallCollector(rel_path)
    collector.visit(tree)
    parsed.imports = collector.imports
    parsed.calls = collector.calls
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
