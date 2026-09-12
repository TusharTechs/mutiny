"""Semantics-preserving local-variable rename.

Gate 2 rule 5: a proof test must still pass on HEAD after the locals of the
target function are renamed. A test that encodes structure rather than
behaviour breaks here; a test that asserts on values does not.

Parameters are deliberately left alone — renaming them would break keyword
calls, which is a behaviour change, not a rename.
"""
from __future__ import annotations

import ast

PREFIX = "_mut_"


def find_function(
    tree: ast.AST, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Resolve "name" or "Class.method". Method names repeat across classes in
    real code -- cachetools defines popitem six times -- so a bare name is only
    usable when it is unique."""
    *owner, name = qualname.split(".")

    def scope(node: ast.AST) -> list[ast.AST]:
        return list(ast.iter_child_nodes(node))

    nodes: list[ast.AST] = [tree]
    for cls in owner:
        found = [
            n for parent in nodes for n in scope(parent)
            if isinstance(n, ast.ClassDef) and n.name == cls
        ]
        if not found:
            return None
        nodes = found

    matches = [
        n for parent in nodes for n in ast.walk(parent)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]
    if owner:
        matches = [
            n for parent in nodes for n in scope(parent)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ] or matches
    if len(matches) != 1:
        return None
    return matches[0]


class _LocalCollector(ast.NodeVisitor):
    """Names bound inside the function body that are safe to rename."""

    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.params = {a.arg for a in _all_args(fn)}
        self.declared_global: set[str] = set()
        self.bound: set[str] = set()
        self._depth = 0

    def visit_Global(self, node: ast.Global) -> None:
        self.declared_global.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        self.declared_global.update(node.names)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested definitions introduce their own scope; do not descend.
        self.bound.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]
    visit_ClassDef = visit_FunctionDef  # type: ignore[assignment]

    @property
    def renameable(self) -> set[str]:
        return self.bound - self.params - self.declared_global


class _Renamer(ast.NodeTransformer):
    def __init__(self, names: set[str]) -> None:
        self.names = names

    def visit_Name(self, node: ast.Name) -> ast.Name:
        if node.id in self.names:
            node.id = PREFIX + node.id
        return node

    def visit_arg(self, node: ast.arg) -> ast.arg:
        return node  # parameters are never renamed


def _all_args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    a = fn.args
    out = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
    if a.vararg:
        out.append(a.vararg)
    if a.kwarg:
        out.append(a.kwarg)
    return out


def rename_locals(source: str, func_name: str) -> str | None:
    """Return `source` with the locals of `func_name` renamed, or None.

    None means the transform could not be applied cleanly — the function was not
    found, has no renameable locals, or the result does not round-trip. Gate 2
    treats None as *skip rule 5*, never as a failure: the proof test must not be
    penalised for a limitation of this transformer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    target = find_function(tree, func_name)
    if target is None:
        return None

    collector = _LocalCollector(target)
    for stmt in target.body:
        collector.visit(stmt)
    names = collector.renameable
    if not names:
        return None

    for stmt in target.body:
        _Renamer(names).visit(stmt)
    ast.fix_missing_locations(tree)

    try:
        renamed = ast.unparse(tree)
        ast.parse(renamed)
    except (SyntaxError, ValueError):
        return None
    return renamed
