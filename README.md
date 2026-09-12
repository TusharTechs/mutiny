# MUTINY

Adversarial verification for AI-written code.

MUTINY attacks the diff in a pull request, runs those attacks in isolated forked
sandboxes, and reports a blind spot **only when it can produce a test that passes
on HEAD and fails on the mutant**.

> Coverage tells you which lines ran. MUTINY tells you which bugs your tests would miss.

Status: pre-alpha. Phase 1 (proof-test gate validation) in progress.

## The loop

```
ATTACK  ->  SURVIVE  ->  PROVE  ->  REPAIR
```

Two gates decide what a developer ever sees:

- **Gate 1 — plausibility.** A mutant must import cleanly, survive the repo's own
  linter and type-checker, and change observable behaviour. Otherwise it is a
  broken build, not a blind spot.
- **Gate 2 — proof.** A surviving mutant is reported only if MUTINY can write a
  test that passes on HEAD and fails on the mutant *by assertion*, touches only
  the public interface, and is invariant to a semantics-preserving rename.

Equivalent mutants can never satisfy Gate 2, so they are dropped by the same
mechanism that produces the evidence. Precision is 100% by construction.

## Licence

Apache-2.0.
