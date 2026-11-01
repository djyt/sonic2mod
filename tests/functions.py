"""Functions as tests/selection.py places a change: which a case ran, which a change touched.

    ran       each executed line belongs to the innermost function whose body holds it (a def,
              decorator or class line belongs to what holds it: it runs as the file loads)
    touched   the functions whose syntax tree differs between two versions of a file, docstrings
              left out; None where anything outside a function differs (a constant, an import, a
              class's fields: every case that loaded the file may move)
    index     {"cases": [names], "functions": {"path::qualname": bitmask over cases, hex}}: each
              function the cases that ran it (a few hundred KB where per-case lists were megabytes)

A function added moves nothing by itself: only a changed caller reaches it.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SEP = "::"


# --- what ran ----------------------------------------------------------------------------------

def ran_functions(path: Path, lines: Iterable[int]) -> list[str]:
    """The qualnames of the functions `lines` of the file at `path` executed in, sorted."""
    spans = _spans(path)
    out = set()
    for line in lines:
        held = [(end - start, name) for start, end, name in spans if start <= line <= end]
        if held:
            out.add(min(held)[1])
    return sorted(out)


@cache
def _spans(path: Path) -> tuple[tuple[int, int, str], ...]:
    """(first body line, last line, qualname) of each function in the file; none if it does not parse."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return ()
    return tuple((node.body[0].lineno, node.end_lineno or node.body[0].lineno, name)
                 for name, node in _named(tree))


# --- what a change touched ---------------------------------------------------------------------

def touched_functions(old: str | None, new: str | None) -> set[str] | None:
    """The qualnames whose code differs from `old` to `new` (a file's two sources); None when
    anything outside a function does, or either is missing or does not parse."""
    if old is None or new is None:
        return None
    try:
        old_tree, new_tree = ast.parse(old), ast.parse(new)
    except SyntaxError:
        return None
    if _skeleton(old_tree) != _skeleton(new_tree):
        return None
    was = {name: _dump(node) for name, node in _named(old_tree)}
    now = {name: _dump(node) for name, node in _named(new_tree)}
    return {name for name, code in was.items() if now.get(name) != code}


def _named(tree: ast.AST) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """(qualname, node) of every function in `tree`, nested ones too: Class.method, outer.inner."""
    out: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _FUNCTIONS):
                out.append((prefix + child.name, child))
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return out


def _skeleton(tree: ast.Module) -> str:
    """The module with every function and docstring taken out: what runs as it loads."""
    bare = copy.deepcopy(tree)
    for node in ast.walk(bare):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            node.body = _undocumented([n for n in body if not isinstance(n, _FUNCTIONS)])   # type: ignore[attr-defined]
    return ast.dump(bare)


def _dump(node: ast.AST) -> str:
    """A function's code, its docstring (and its nested functions' docstrings) left out."""
    bare = copy.deepcopy(node)
    for inner in ast.walk(bare):
        if isinstance(inner, _FUNCTIONS):
            inner.body = _undocumented(inner.body) or [ast.Pass()]
    return ast.dump(bare)


def _undocumented(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


# --- the index -----------------------------------------------------------------------------------

def encode_index(ran: Mapping[str, Mapping[str, Iterable[str]]]) -> dict:
    """{case: {path: qualnames}} as the index: each function a bitmask over the sorted cases."""
    cases = sorted(ran)
    masks: dict[str, int] = {}
    for bit, case in enumerate(cases):
        for path, names in ran[case].items():
            for name in names:
                key = f"{path}{_SEP}{name}"
                masks[key] = masks.get(key, 0) | 1 << bit
    return {"cases": cases, "functions": {key: f"{mask:x}" for key, mask in sorted(masks.items())}}


def decode_index(index: Mapping) -> dict[str, dict[str, set[str]]]:
    """The index back as {case: {path: qualnames}}."""
    cases = list(index.get("cases", ()))
    out: dict[str, dict[str, set[str]]] = {case: {} for case in cases}
    for key, mask in (index.get("functions") or {}).items():
        path, _, name = key.partition(_SEP)
        bits = int(mask, 16)
        for bit, case in enumerate(cases):
            if bits >> bit & 1:
                out[case].setdefault(path, set()).add(name)
    return out
