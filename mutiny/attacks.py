"""Attack synthesis — ask Nano for semantic mutations, then validate them.

The model proposes bug classes, not operator swaps: a dropped guard, a boundary
that drifts by one, a comparison that loses its tie-break. Everything it returns
is validated against the actual source before it costs anything, because a model
that misreports a line number produces a mutation that cannot be applied.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from .models import NANO, NemotronClient
from .mutant import Mutation, MutationError
from .rename import find_function

BUG_CLASSES = [
    "dropped_guard", "boundary_drift", "numeric_type", "temporal",
    "argument_transposition", "swallowed_failure", "concurrency",
    "identity_and_keys", "state_leakage", "comparison_inversion",
]

SYSTEM = """You introduce realistic bugs into working Python, of the kind that
reach production and pass review — not random operator swaps.

For each mutation give the EXACT substring to replace and its replacement. The
substring must appear on the line you name, exactly once, character for character
including spacing. Prefer changes that are plausible as a human or AI mistake and
that a typical test suite would not exercise: boundary conditions, dropped
guards, lost tie-breaks, transposed arguments, swallowed failures.

Avoid: changing comments or docstrings, changes that cannot compile, and changes
so large the diff is obviously wrong.

Reply with JSON only: {"mutations": [{"line": int, "original": str,
"mutated": str, "bug_class": str, "rationale": str}]}
bug_class must be one of: """ + ", ".join(BUG_CLASSES)

USER = """File `{path}`, function `{function}`:

```python
{numbered}
```

Produce {n} distinct mutations inside `{function}`. Line numbers are the real
ones from the file, shown in the left column.{restriction}"""

RESTRICTION = """

Only mutate the lines marked with `>` in the left gutter. Those are the lines
this change introduced, and they are the only ones under review. Lines without a
marker are existing code shown for context — do not touch them."""

_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def numbered_source(
    source: str, start: int, end: int, mark: tuple[int, ...] = ()
) -> str:
    """Line-numbered listing; lines in `mark` get an arrow so the model can see
    which ones the change actually touched."""
    lines = source.splitlines()
    marked = set(mark)
    out = []
    for i in range(start, min(end, len(lines)) + 1):
        gutter = ">" if i in marked else " "
        out.append(f"{gutter}{i:5d} | {lines[i - 1]}")
    return "\n".join(out)


def function_span(source: str, name: str) -> tuple[int, int]:
    """Line range of `name`, which may be "func" or "Class.method"."""
    node = find_function(ast.parse(source), name)
    if node is None:
        raise ValueError(f"{name!r} not found, or ambiguous — qualify it as Class.method")
    return node.lineno, (node.end_lineno or node.lineno)


def _parse(text: str) -> list[dict]:
    blocks = _FENCE.findall(text)
    raw = max(blocks, key=len) if blocks else text
    raw = raw.strip()
    start = raw.find("{")
    if start > 0:
        raw = raw[start:]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    items = data.get("mutations", data) if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def generate_mutations(
    client: NemotronClient,
    repo: Path,
    rel_path: str,
    function: str,
    n: int = 8,
    model: str = NANO,
    max_tokens: int = 14000,
    only_lines: tuple[int, ...] | None = None,
) -> tuple[list[Mutation], list[str]]:
    """Return validated mutations plus the reasons any candidate was dropped."""
    source = (repo / rel_path).read_text(encoding="utf-8")
    lo, hi = function_span(source, function)

    text, _ = client.complete(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                path=rel_path, function=function,
                numbered=numbered_source(source, lo, hi, only_lines or ()),
                n=n,
                restriction=RESTRICTION if only_lines else "")},
        ],
        model=model, max_tokens=max_tokens, temperature=0.4,
        tag=f"attacks:{rel_path}:{function}",
    )

    mutations: list[Mutation] = []
    rejected: list[str] = []
    for i, item in enumerate(_parse(text)):
        try:
            m = Mutation(
                path=rel_path, line=int(item["line"]),
                original=str(item["original"]), mutated=str(item["mutated"]),
                bug_class=str(item.get("bug_class", "unknown")),
                rationale=str(item.get("rationale", ""))[:200],
                id=f"{Path(rel_path).stem}.{function}.{i}",
            )
        except (KeyError, ValueError, TypeError, MutationError) as exc:
            rejected.append(f"malformed: {exc}")
            continue
        if not (lo <= m.line <= hi):
            rejected.append(f"{m.id}: line {m.line} outside {function} ({lo}-{hi})")
            continue
        if only_lines and m.line not in only_lines:
            rejected.append(f"{m.id}: line {m.line} is context, not part of the change")
            continue
        try:
            m._patched_source(repo)  # proves the span exists exactly once
        except MutationError as exc:
            rejected.append(f"{m.id}: {str(exc).splitlines()[0]}")
            continue
        mutations.append(m)
    return mutations, rejected
