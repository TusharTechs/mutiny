"""Say in plain English what changed, grounded in what actually executed.

A witness is precise and nearly unreadable:

    v = Version(1,2,3,'rc'); vb = v.bump_prerelease(); (vb._major, ..., vb._prerelease)
      before: (1, 2, 3, 'rc')
      after:  (1, 2, 3, 'rc.0')

A reviewer wants the sentence: *a version whose prerelease has no number now
gains `.0` where it used to be left alone.*

The explanation is layered over the evidence and never replaces it. Nemotron is
given only the diff and the observed before/after pairs, and asked to describe
what the outputs show — not to guess intent, not to judge whether the change is
correct, and not to mention anything it cannot point at. The witness is always
printed underneath, so a reader can check the sentence against the fact.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .models import SUPER, NemotronClient

SYSTEM = """You describe a behaviour change in one or two plain sentences, for
an engineer reviewing a pull request.

You are given a code change and a set of inputs that produce different results
before and after it. Those results are measured facts. Describe what the change
does to the function's observable behaviour, in terms of inputs and outputs, so
that a reader who has not studied the diff understands what will differ for
callers.

Rules:
- Describe only what the before/after pairs demonstrate. Never speculate about
  intent, correctness, or whether it is a bug.
- Name the condition that triggers the difference, as specifically as the
  evidence allows: which inputs, which state, which edge.
- Prefer concrete values from the evidence over abstractions.
- No preamble, no restating the diff line by line, no markdown headings.
- Two sentences at most. One is usually better."""

USER = """Function `{function}`.

The change:
```diff
{diff}
```

Inputs whose results differ:
```
{witnesses}
```

Describe the behaviour change."""


@dataclass(frozen=True)
class Explanation:
    text: str
    cost_usd: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.text.strip())


def _witness_block(divergences: list[dict], limit: int = 6) -> str:
    lines = []
    for d in divergences[:limit]:
        lines.append(f"{d['input']}")
        lines.append(f"  before: {d['before']}")
        lines.append(f"  after:  {d['after']}")
    if len(divergences) > limit:
        lines.append(f"... and {len(divergences) - limit} further inputs")
    return "\n".join(lines)


def explain(
    client: NemotronClient,
    function: str,
    diff: str,
    divergences: list[dict],
    model: str = SUPER,
    max_tokens: int = 4000,
) -> Explanation:
    """One or two sentences describing what the witnesses show."""
    if not divergences:
        return Explanation("")
    try:
        text, call = client.complete(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": USER.format(
                 function=function, diff=diff[:3000],
                 witnesses=_witness_block(divergences))}],
            model=model, max_tokens=max_tokens, temperature=0.2,
            tag=f"explain:{function}",
        )
    except Exception:  # noqa: BLE001 - an explanation is a nicety, evidence is not
        return Explanation("")

    cleaned = " ".join(text.strip().split())
    for fence in ("```", "`"):
        cleaned = cleaned.replace(fence, "")
    return Explanation(cleaned[:600], getattr(call, "cost_usd", 0.0))


def summarise_review(findings: list[tuple[str, str]]) -> str:
    """A one-line headline for a whole change, from per-function explanations."""
    if not findings:
        return "No behaviour change detected."
    if len(findings) == 1:
        return f"{findings[0][0]} behaves differently."
    names = ", ".join(name for name, _ in findings[:3])
    more = f" and {len(findings) - 3} more" if len(findings) > 3 else ""
    return f"{len(findings)} functions behave differently: {names}{more}."


def as_json(function: str, explanation: Explanation, divergences: list[dict]) -> str:
    """Machine-readable form, for posting into a pull request."""
    return json.dumps({
        "function": function,
        "summary": explanation.text,
        "witnesses": divergences,
    }, indent=2)
