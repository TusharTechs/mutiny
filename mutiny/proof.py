"""Ask Nemotron for a proof test, then let Gate 2 decide whether it is one.

The model proposes; execution disposes. Nothing here trusts the model's claim
that its test distinguishes the mutant — that is settled by running it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .attacks import focused_module
from .gate2 import GateResult, RuleResult, verify
from .models import SUPER, NemotronClient
from .mutant import Mutation

SYSTEM = """You write a single pytest test that proves a specific bug exists.

You are given a function, a mutation of it that the existing test suite fails to
catch, and the current tests. Write one test that PASSES on the original code and
FAILS on the mutated code, so that it would have caught this bug.

Hard requirements — a test violating any of these is rejected:
1. It must fail on the mutant via a plain `assert` that evaluates false. Not by
   raising, not by crashing, not via pytest.raises.
2. Use only the module's public interface. No `inspect`, no `dis`, no reading
   source, no private (underscore-prefixed) names or attributes, no traceback or
   frame walking.
3. Assert on returned values, not on call counts or mock protocols.
4. Deterministic: no randomness, no clock, no network, no filesystem.
5. Do not quote or mention the mutation. The test must read as a test someone
   would have written anyway.

Return ONLY the complete test file inside a single ```python code block. Include
the imports it needs. Name the test function descriptively after the behaviour
it checks, never after the bug or the mutation."""

USER = """Module `{module}` ({path}):
```python
{module_source}
```

{tests_block}

The mutation that survives them, at line {line}:
```diff
- {original}
+ {mutated}
```
It sits in `{function}`. Bug class: {bug_class}.

Write the test that catches it."""

RETRY = """That test was rejected. {reason}

Write a different test that fixes this. Same requirements as before."""

COVERING = """These existing tests already execute the mutated line. Every one of them still
passes after the mutation — that is exactly the gap you are closing. They also
show how a caller reaches this code, so follow their approach:

```python
{examples}
```"""

FALLBACK = """The existing tests for this module (they all pass on both versions — that is the
problem):
```python
{test_source}
```"""

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class ProofAttempt:
    source: str
    gate: GateResult
    attempt: int
    cost_usd: float
    seconds: float
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.gate.passed


def extract_code(text: str) -> str:
    blocks = _FENCE.findall(text)
    if blocks:
        return max(blocks, key=len).strip() + "\n"
    return text.strip() + "\n"


def generate_proof_test(
    client: NemotronClient,
    repo: Path,
    mutation: Mutation,
    module_import_name: str,
    target_function: str,
    test_path: str | None = None,
    covering_tests: list[tuple[str, str]] | None = None,
    proof_test_path: str = "tests/test_mutiny_proof.py",
    model: str = SUPER,
    max_attempts: int = 3,
    max_tokens: int = 3000,
    repeats: int = 1,
    python_exe: str | None = None,
) -> list[ProofAttempt]:
    """Generate, verify, and on failure re-ask with the gate's own complaint."""
    import sys

    whole = (repo / mutation.path).read_text(encoding="utf-8")
    module_source = focused_module(whole, target_function)

    # The tests that already run this line are the single most useful thing we can
    # show: without them the model cannot tell how a caller reaches internal code,
    # and it writes a sound test of an adjacent function the mutation never touches.
    if covering_tests:
        examples = "\n\n".join(f"# {tid}\n{src}" for tid, src in covering_tests)
        tests_block = COVERING.format(examples=examples)
    elif test_path:
        tests_block = FALLBACK.format(
            test_source=(repo / test_path).read_text(encoding="utf-8"))
    else:
        tests_block = "No existing tests were found for this code."

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER.format(
            module=module_import_name, path=mutation.path,
            module_source=module_source, tests_block=tests_block,
            line=mutation.line, original=mutation.original.strip(),
            mutated=mutation.mutated.strip(), function=target_function,
            bug_class=mutation.bug_class,
        )},
    ]

    attempts: list[ProofAttempt] = []
    for n in range(1, max_attempts + 1):
        text, call = client.complete(
            messages, model=model, max_tokens=max_tokens,
            temperature=0.3 if n > 1 else 0.0,
            tag=f"proof:{mutation.id or mutation.path}:{n}",
        )
        source = extract_code(text)
        if not source.strip():
            attempts.append(ProofAttempt(
                "", GateResult((RuleResult(
                    "0 model returned no content", "fail",
                    f"truncated at {max_tokens} tokens with empty content and empty "
                    f"reasoning — the prompt is too large, not the allowance too small",
                ),)), n, call.cost_usd, call.seconds, call.truncated))
            break
        gate = verify(
            repo, mutation, source, proof_test_path,
            target_function=target_function,
            python_exe=python_exe or sys.executable,
            repeats=repeats,
        )
        attempts.append(ProofAttempt(source, gate, n, call.cost_usd, call.seconds, call.truncated))
        if gate.passed:
            break

        reason = "; ".join(f"{r.name}: {r.detail or 'failed'}" for r in gate.failures)
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": RETRY.format(reason=reason)},
        ]

    return attempts
