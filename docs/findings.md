# Phase 1 findings — 12 September 2026

The go/no-go experiment: can Nemotron write tests that clear Gate 2, and does
Gate 2 hold up when a capable model is actively trying to satisfy it?

Answer to the first: **yes, and cheaply.** The boundary mutation was proven on
the first attempt for $0.0004 — Super wrote the obvious correct test.

Answer to the second: **not at first.** The model found three ways through the
gate within the first hour. Two were genuine gaming and are now closed. The
third was not gaming at all, and it changed how the product should be described.

## Escape 1 — object identity

```python
value = lo + 0.0            # "equal to lo but a distinct float object"
assert clamp(value, lo) is value
```

HEAD returns `value`, the mutant returns `lo`. Same number, different object, so
`is` separates them. No caller of a numeric clamp depends on which object comes
back. **Closed:** `is` / `is not` against anything but `None`/`True`/`False` is
now rejected as an implementation-detail assertion.

## Escape 2 — adversarial doubles

```python
class _Dummy:
    def __lt__(self, other): return False
    def __le__(self, other): return True
```

A type whose `<` and `<=` disagree by construction separates those operators
without saying anything about the inputs the function is written for. This is a
strong attractor: in a later run against a genuinely equivalent mutation, three
of four attempts reached for an `__add__`-overriding double. **Closed:** a proof
test may not define a class overriding comparison, coercion or container
dunders. Plain data holders are still allowed.

Related tell, also closed: the generated tests narrated the diff in their
comments ("Original implementation... Mutated version..."). A proof test should
read as a test someone would have written anyway.

## Escape 3 — negative zero, and why it is not an escape

```python
assert math.copysign(1.0, clamp(-0.0, 0.0)) == -1.0
```

`-0.0 < 0.0` is False so HEAD returns `-0.0`; `-0.0 <= 0.0` is True so the mutant
returns `0.0`. The two compare equal but `copysign` distinguishes them. Public
API, stdlib only, deterministic, no doubles.

This is real executable proof, and the mutation we had labelled *equivalent* is
therefore **not equivalent**. The gate was right and our fixture was wrong.

### What that changes

Three corrections to how this project describes itself:

1. **True equivalence is rarer than the literature's framing suggests.** Most
   "equivalent" mutants are actually distinguishable through some edge of the
   type system. We stopped claiming to solve equivalent-mutant detection.

2. **The gate proves "this is a real behavioural difference." It does not prove
   "this is worth your time."** Those are different questions, and conflating
   them was the error. Precision-by-construction holds for the first claim only.

3. **Triage is therefore a first-class product problem, not a nicety.** A
   developer would close the `-0.0` finding as noise even though it is
   provably real. This is what severity ranking — and the Tavily bug-class
   priors — actually exist to solve. It moves them off the "nice to have" list.

## Rule 5 is narrower than specified

Once rules 3 and 4 ban introspection and private access, local variable names
are almost invisible to behaviour, so a rename has little left to break. The
class it does catch is real: reaching locals through
`sys.exc_info()[2].tb_next.tb_frame.f_locals` passes every static check. Keep
the rule, describe it accurately as a backstop rather than a general
structure-detector.

## Method note

Every escape above was found by pointing the model at the gate and letting it
try, not by reasoning about the gate in the abstract. Budget for this to
continue: the rule set is not finished, and each new bug class is a new surface.
