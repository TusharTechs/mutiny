"""Turn a witness into a test the repository keeps.

A finding is a moment. Somebody reads it, decides the new behaviour is what they
meant, and merges -- and nothing stops the next refactor from moving it back.
The observation that cost a sandbox and half a cent evaporates.

Pinning writes it down. Each generated test asserts a result that was *observed
by executing the code*, which is the only kind of assertion this project is
willing to produce: the rule everywhere else is that the model writes inputs and
never assertions, because a wrong assertion manufactures a false finding out of
nothing. Here the value comes from a real run, and a human chose which side of
the change was correct before it was written down.

That is also why this is not "AI writes your tests". Nothing here is predicted.
The inputs were generated, executed, and kept only if they ran; the expected
values are what actually came back.
"""
from __future__ import annotations

import ast
import datetime as _datetime
import re

HEADER = '''"""Behaviour pinned by MUTINY{source}.

Every assertion below is a value that was observed by running the code, not a
prediction: the input was generated, executed against both versions of
{qualname}, and kept because the two disagreed. A human chose the {side}
side as correct.

Regenerate with:

    mutiny pin --url {url} --side {side}
"""
import pytest  # noqa: F401 - used by the raises cases below

{imports}
'''

# A value whose repr we cannot faithfully reproduce in a plain assertion:
# canonicalised containers were sorted for comparison, and addresses were
# stripped. Pinning either would write a test that fails for the wrong reason.
UNSTABLE = re.compile(r"0xADDR|^<[\w.]+ object|^frozenset\(|^\{")

ERROR = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\s*:\s*(.*)$")

# Provided to every probe by the driver, so they are modules rather than names
# belonging to the code under test.
PROVIDED = {"asyncio", "math", "datetime"}


def _free_names(snippet: str) -> set[str]:
    """Names a snippet reads without binding them first.

    `from module import *` will not do: it skips anything underscore-prefixed,
    and an internal helper is exactly what a change like this usually touches.
    The first generated test failed on NameError for `_is_source`.
    """
    try:
        tree = ast.parse(snippet)
    except SyntaxError:
        return set()

    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Lambda):
            bound.update(arg.arg for arg in node.args.args)
        elif isinstance(node, ast.comprehension):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    bound.add(target.id)

    builtins = set(dir(__builtins__)) if isinstance(__builtins__, dict) is False else set(__builtins__)
    read = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return {name for name in read - bound - builtins if not name.startswith("__")}


def _identifier(expression: str, used: set[str]) -> str:
    """A readable, unique test name derived from the probe."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", expression).strip("_").lower()[:60]
    slug = slug or "probe"
    name = f"test_{slug}"
    suffix = 2
    while name in used:
        name = f"test_{slug}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _split(snippet: str) -> tuple[list[str], str] | None:
    """Leading statements, and the trailing expression that produced the value."""
    try:
        tree = ast.parse(snippet)
    except SyntaxError:
        return None
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return None
    head = [ast.unparse(node) for node in tree.body[:-1]]
    return head, ast.unparse(tree.body[-1].value)


def _case(divergence: dict, side: str, used: set[str]) -> str | None:
    value = str(divergence.get(side, ""))
    if not value or UNSTABLE.search(value):
        return None
    parts = _split(divergence["input"])
    if parts is None:
        return None
    head, tail = parts

    lines = [f"def {_identifier(divergence['input'], used)}():"]
    for statement in head:
        lines.append(f"    {statement}")

    raised = ERROR.match(value)
    if raised:
        lines.append(f"    with pytest.raises({raised.group(1)}):")
        lines.append(f"        {tail}")
    else:
        lines.append(f"    assert repr({tail}) == {value!r}")
    return "\n".join(lines)


def pin(
    divergences: list[dict],
    *,
    module: str,
    qualname: str,
    side: str = "after",
    url: str = "",
) -> tuple[str, int]:
    """Generate a test file pinning one side of a finding. Returns (text, count)."""
    if side not in ("before", "after"):
        raise ValueError("side must be 'before' or 'after'")

    used: set[str] = set()
    kept = [d for d in divergences if _case(d, side, set(used)) is not None]
    used = set()
    cases = [case for case in (_case(d, side, used) for d in divergences) if case]

    wanted: set[str] = set()
    for divergence in kept:
        wanted |= _free_names(divergence["input"])
    modules = sorted(wanted & PROVIDED)
    from_module = sorted(wanted - PROVIDED)
    lines = [f"import {name}" for name in modules]
    if from_module:
        lines.append(f"from {module} import {', '.join(from_module)}")
    imports = "\n".join(lines) or f"import {module}  # noqa: F401"

    header = HEADER.format(
        source=f" from {url}" if url else "",
        qualname=qualname, side=side, url=url or "<pull request url>",
        imports=imports)

    if not cases:
        return header + "\n# No witness could be pinned: every observed value was a\n" \
                        "# canonicalised container or an object identity, which cannot\n" \
                        "# be asserted faithfully in a plain test.\n", 0

    skipped = len(divergences) - len(cases)
    body = "\n\n\n".join(cases)
    note = ""
    if skipped:
        note = (f"\n\n# {skipped} further witness{'' if skipped == 1 else 'es'} "
                f"could not be pinned: the observed value was a canonicalised\n"
                f"# container or an object identity.\n")
    return f"{header}\n\n{body}\n{note}", len(cases)
