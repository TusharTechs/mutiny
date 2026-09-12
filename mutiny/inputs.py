"""Ask Nemotron for call expressions that exercise a function hard.

This is the model's whole job in the differential design, and it is a much
easier job than writing a proof test. A bad input costs one execution on each
side and contributes nothing; a wrong assertion, by contrast, produces a false
finding. Getting it right most of the time is not required — coverage is.
"""
from __future__ import annotations

import ast
import re

from .models import NANO, NemotronClient

SYSTEM = """You produce Python call expressions that exercise a function across
its interesting behaviour.

Each expression is evaluated inside the module's own namespace, so every public
name in that module is already available to you — classes, constants, helpers.
No imports, no assignments, no statements: one expression per line, nothing else.

Aim for the edges, because that is where two versions of a function differ:
boundary values, empty and single-element collections, zero and negative
numbers, None where it is permitted, values that differ only in case or
whitespace, the largest and smallest plausible values, strings that nearly
parse, and ordinary well-formed values for contrast.

Expressions that raise are fine and useful — a rejection is behaviour too. Do
not attempt anything that touches the network, the filesystem, the clock or
randomness, and never write an infinite loop.

Return only a fenced python block containing one expression per line."""

USER = """Module `{module}`, the function under test is `{qualname}`:

```python
{source}
```

{extra}Write {n} distinct call expressions that reach `{qualname}`. If it is a
method, construct the receiver inline as part of the expression."""

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def _valid(expr: str) -> bool:
    """A single expression, no statements, no obvious escape hatches."""
    expr = expr.strip()
    if not expr or expr.startswith("#"):
        return False
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return False
    banned = {"open", "exec", "eval", "compile", "__import__", "input",
              "exit", "quit", "breakpoint"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in banned:
            return False
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return False
    return True


def extract(text: str) -> list[str]:
    blocks = _FENCE.findall(text)
    body = max(blocks, key=len) if blocks else text
    seen: dict[str, None] = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if _valid(line):
            seen.setdefault(line, None)
    return list(seen)


def generate(
    client: NemotronClient,
    module: str,
    qualname: str,
    source: str,
    n: int = 40,
    hint: str = "",
    model: str = NANO,
    max_tokens: int = 14000,
) -> list[str]:
    text, _ = client.complete(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                module=module, qualname=qualname, source=source, n=n,
                extra=(hint + "\n\n") if hint else "")},
        ],
        model=model, max_tokens=max_tokens, temperature=0.7,
        tag=f"inputs:{module}:{qualname}",
    )
    return extract(text)
