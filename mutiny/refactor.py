"""Ask Nemotron to refactor a function, then check whether it still behaves.

This is the product's real setting. Detecting human `fix:` commits measures
something harder and narrower than what anyone would buy: those are deliberate
changes, often on edge paths that took a bug report to find. The question that
matters is whether an agent's refactor quietly broke something.

The existing test suite is the oracle that makes this honest. A refactor the
suite rejects needs no help from us. The interesting population is the refactors
that *pass the tests* — every divergence found there is a behaviour change the
suite failed to catch.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from .source import focused_module, function_span
from .models import SUPER, NemotronClient

SYSTEM = """You refactor Python for readability, preserving behaviour exactly.

Typical work: clearer names for locals, simpler conditionals, early returns,
comprehensions in place of accumulation loops, extracted intermediate values,
removed duplication, straightened control flow.

Keep the signature, the name and the semantics identical. Callers must not be
able to tell the difference for any input at all, including malformed ones and
edge cases.

Return only the complete rewritten function in a single fenced python block, at
module indentation level — no surrounding class, no imports, no commentary."""

USER = """Refactor `{qualname}` for readability. Behaviour must not change.

```python
{source}
```"""

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class Refactor:
    qualname: str
    original: str
    rewritten: str
    cost_usd: float = 0.0

    @property
    def changed(self) -> bool:
        return self.original.strip() != self.rewritten.strip()


def _dedent_to(block: str, indent: str) -> str:
    lines = block.splitlines()
    if not lines:
        return block
    base = len(lines[0]) - len(lines[0].lstrip())
    return "\n".join(indent + ln[base:] if ln.strip() else "" for ln in lines)


def extract_function(text: str, name: str) -> str | None:
    """The rewritten function body, or None if the reply does not contain one."""
    blocks = _FENCE.findall(text)
    candidate = max(blocks, key=len) if blocks else text
    candidate = candidate.strip()
    try:
        tree = ast.parse(candidate)
    except SyntaxError:
        return None
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(defs) != 1:
        return None
    return candidate


def apply(source: str, qualname: str, rewritten: str) -> str | None:
    """Replace `qualname`'s definition in `source`, preserving its indentation."""
    try:
        lo, hi = function_span(source, qualname)
    except ValueError:
        return None
    lines = source.splitlines()
    indent = lines[lo - 1][: len(lines[lo - 1]) - len(lines[lo - 1].lstrip())]
    patched = lines[: lo - 1] + _dedent_to(rewritten, indent).splitlines() + lines[hi:]
    out = "\n".join(patched) + ("\n" if source.endswith("\n") else "")
    try:
        ast.parse(out)
    except SyntaxError:
        return None
    return out


def refactor(
    client: NemotronClient,
    source: str,
    qualname: str,
    model: str = SUPER,
    max_tokens: int = 16000,
    temperature: float = 0.4,
) -> Refactor | None:
    focused = focused_module(source, qualname)
    lo, hi = function_span(source, qualname)
    original = "\n".join(source.splitlines()[lo - 1 : hi])

    text, call = client.complete(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": USER.format(qualname=qualname, source=focused)}],
        model=model, max_tokens=max_tokens, temperature=temperature,
        tag=f"refactor:{qualname}",
    )
    rewritten = extract_function(text, qualname.rsplit(".", 1)[-1])
    if rewritten is None:
        return None
    return Refactor(qualname, original, rewritten, call.cost_usd)
