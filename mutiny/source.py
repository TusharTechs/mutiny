"""Reading a source file: locating a function and showing just enough of it.

Extracted from the mutation-testing design this project started as. Only the
parts the differential product actually uses survive — finding a function by
qualified name, and rendering a focused view of it.
"""
from __future__ import annotations

import ast

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
    # A typing.overload stub is a signature, not an implementation: tenacity
    # declares `retry` four times, three of them `@t.overload` with a body of
    # `...`. Counting those made the real function ambiguous and unreachable.
    real = [m for m in matches if not _is_overload(m)]
    if len(real) == 1:
        return real[0]
    if len(matches) != 1:
        return None
    return matches[0]


def _is_overload(node) -> bool:
    """Is this a typing.overload signature stub rather than an implementation?"""
    for decorator in getattr(node, "decorator_list", []):
        name = (decorator.attr if isinstance(decorator, ast.Attribute)
                else getattr(decorator, "id", ""))
        if name == "overload":
            return True
    return False


def function_span(source: str, name: str) -> tuple[int, int]:
    """Line range of `name`, which may be "func" or "Class.method"."""
    node = find_function(ast.parse(source), name)
    if node is None:
        raise ValueError(f"{name!r} not found, or ambiguous — qualify it as Class.method")
    return node.lineno, (node.end_lineno or node.lineno)


def focused_module(
    source: str, qualname: str, max_header: int = 70, pad: int = 6
) -> str:
    """The module header plus the target function, not the whole file.

    Shipping an entire module costs thousands of tokens and actively hurts: given
    1,000 lines of context for a task that concerns one function, Nemotron reasons
    until it exhausts its output allowance and returns nothing at all.
    """
    lines = source.splitlines()
    try:
        lo, hi = function_span(source, qualname)
    except ValueError:
        # Falling back to the whole file is what we are trying to avoid: an
        # ambiguous bare name like __setitem__, which cachetools defines eight
        # times, would silently restore the 800-line prompt.
        raise ValueError(
            f"cannot focus on {qualname!r} — qualify it as Class.method"
        ) from None

    import ast as _ast

    header_end = 0
    for node in _ast.parse(source).body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom, _ast.Assign, _ast.Expr)):
            header_end = max(header_end, node.end_lineno or node.lineno)
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            break
    header_end = min(header_end, max_header)

    start = max(header_end + 1, lo - pad)
    end = min(len(lines), hi + pad)

    out = lines[:header_end]
    if start > header_end + 1:
        out.append(f"\n# ... {start - header_end - 1} lines elided ...\n")
    out += lines[start - 1 : end]
    if end < len(lines):
        out.append(f"\n# ... {len(lines) - end} lines elided ...")
    return "\n".join(out)


def class_source(source: str, name: str, max_lines: int = 80) -> str:
    """A class's signature surface: bases, `__init__`, and what it binds.

    The whole class is usually far too much -- sqlalchemy's InstanceState runs to
    hundreds of lines -- and almost none of it says how to build one. What a
    caller needs is the bases, the constructor, and the attributes set on the
    instance.
    """
    tree = ast.parse(source)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            found = node
            break
    if found is None:
        return ""

    bases = ", ".join(ast.unparse(b) for b in found.bases)
    lines = [f"class {found.name}({bases}):" if bases else f"class {found.name}:"]

    for statement in found.body:
        if isinstance(statement, ast.Assign) and len(lines) < max_lines:
            lines.append("    " + ast.unparse(statement))
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.name == "__init__" or statement.name.startswith("__new__"):
                body = ast.unparse(statement).splitlines()[:max_lines - len(lines)]
                lines += ["    " + line for line in body]
            elif len(lines) < max_lines:
                args = ast.unparse(statement.args)
                lines.append(f"    def {statement.name}({args}): ...")
    return "\n".join(lines[:max_lines])
