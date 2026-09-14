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
    # Whether the change the evidence shows is one the author said they were
    # making. None when the change described itself in no useful way.
    described: bool | None = None

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    @property
    def headline(self) -> str:
        """What a reviewer should take from this, in one clause."""
        if self.described is True:
            return "behaves differently, as the change describes"
        if self.described is False:
            return "behaves differently in a way the change does not mention"
        return "behaves differently"


INTENT_SYSTEM = """You are told what a code change SAYS it does, and shown a
behaviour difference that was measured by running both versions.

Answer one question: is the measured difference something the description
predicts?

Answer YES if the description mentions this behaviour, or if the difference is
an obvious consequence of what it says it is doing. A change that says it fixes
a function returning the wrong value predicts that function returning a
different value.

Answer NO if the description claims no behaviour change at all — a refactor, a
cleanup, a rename, a typing change — or describes something unrelated to what
was measured.

Answer UNCLEAR if the description is empty, or too vague to predict anything.

Reply with exactly one word: YES, NO, or UNCLEAR."""

INTENT_USER = """What the change says about itself:
---
{intent}
---

The measured difference in `{function}`:
{witnesses}

Does the description predict this? YES, NO, or UNCLEAR."""


def _witness_block(divergences: list[dict], limit: int = 6) -> str:
    lines = []
    for d in divergences[:limit]:
        lines.append(f"{d['input']}")
        lines.append(f"  before: {d['before']}")
        lines.append(f"  after:  {d['after']}")
    if len(divergences) > limit:
        lines.append(f"... and {len(divergences) - limit} further inputs")
    return "\n".join(lines)


def _describes(
    client: NemotronClient, function: str, intent: str, divergences: list[dict],
    model: str = SUPER,
) -> bool | None:
    """Did the change predict the difference we measured?

    This is the question that makes a finding worth reading. A pull request
    titled "fix bump_build returning an unchanged version" changing what
    bump_build returns is the fix working, and saying "behaviour changed" about
    it tells a reviewer nothing they did not already know. The same evidence
    under "refactor: no functional change" is the whole point of the tool.
    """
    if not intent.strip():
        return None
    try:
        text, _ = client.complete(
            [{"role": "system", "content": INTENT_SYSTEM},
             {"role": "user", "content": INTENT_USER.format(
                 intent=intent[:1200], function=function,
                 witnesses=_witness_block(divergences, limit=4))}],
            model=model, max_tokens=2000, temperature=0.0,
            tag=f"intent:{function}",
        )
    except Exception:  # noqa: BLE001 - the witness stands without this
        return None
    answer = text.strip().upper()
    if "YES" in answer[:12]:
        return True
    if "NO" in answer[:12]:
        return False
    return None


def explain(
    client: NemotronClient,
    function: str,
    diff: str,
    divergences: list[dict],
    model: str = SUPER,
    max_tokens: int = 4000,
    intent: str = "",
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
    described = _describes(client, function, intent, divergences, model=model)
    return Explanation(cleaned[:600], getattr(call, "cost_usd", 0.0), described)


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
