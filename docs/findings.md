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

---

# Phase 1 on real repositories — 12 September 2026

Three mature libraries, six target functions, 30 mutations that applied cleanly.
semver (362 tests), cachetools (333), packaging (62,434).

| | |
|---|---|
| mutations applied | 30 |
| killed by the existing suite | 27 |
| survivors | 3 |
| of those, provable | 1 |
| run cost | $0.22 |
| wall clock | 942s |

## The result that matters is not the pass rate

**27 of 30 attacks died.** On mature, heavily tested libraries MUTINY finds
almost nothing — which is the *correct* answer and a strategic one. A product
pointed at well-maintained code is a product with nothing to say. It confirms
the PR-scoped design: the gaps are in code written this week, not in
`packaging._cmpkey`. It also means the benchmark must not be "point it at ten
famous libraries", because the honest result there is a row of zeroes.

## One real blind spot, and it is a good one

`cachetools`, `TTLCache.expire`, line 561 — `curr.next` becomes `curr.prev`, a
linked-list traversal reversal. All 333 tests still pass. Nemotron proved it on
the second attempt:

```python
timer = Timer()
cache = TTLCache(maxsize=3, ttl=10, timer=timer)
cache['a'] = 1; timer.time = 1
cache['b'] = 2; timer.time = 2
cache['c'] = 3; timer.time = 12   # everything has expired
assert len(cache.expire()) == 3
```

A cache that silently stops evicting expired entries is a serious bug, the test
is exactly what a careful engineer would write, and it drives the library's own
public timer injection rather than reaching inside. Worth noting the
adversarial-double rule did *not* fire on the `Timer` class here: it defines only
`__init__` and `__call__`, so it is a legitimate test double rather than an
operator-semantics hack. The rule is calibrated about right.

## Of the other two "survivors", one was never a survivor

`SupportsInt -> SupportsFloat` — inside a type annotation. Python does not
evaluate annotations, so no test can ever distinguish it. It consumed a full
suite run and three proof-test attempts before Gate 2 could say so.

**Fixed:** Gate 1 now strips annotations from both versions and compares the
ASTs, rejecting any mutation whose only effect is on code Python never executes.
Caught at Gate 1 this costs nothing; caught at Gate 2 it cost about a minute and
three Super calls.

The third survivor failed rules 1 and 2 — the generated test did not pass on HEAD
either, so the model simply failed to write a working test in three attempts.

## Honest reading of the pass rate

The run prints 1/3 = 33%. Discounting the annotation mutation, which was never
provable, it is 1/2. With n=2 neither number means anything, and quoting 33% as
a result would be dishonest in both directions. **Phase 1 does not have the
sample size to produce a pass rate.** What it establishes is that the pipeline
runs end to end on code we did not write, that it produces at least one genuine
find on a real library, and that cost is not a constraint.

Getting a real denominator requires targets with more gaps than `packaging` has:
recent PRs, newly added modules, and less mature projects.

## Cost and time

$0.22 for the run, $0.37 lifetime against a $30 budget. Model spend is not the
constraint; **wall clock is.** 942s for 30 mutations, almost entirely survivor
detection — `packaging._parse_letter_version` alone took 457s because its suite
runs 62,434 tests per mutant.

That number is the argument for the architecture, and it should go in the
benchmark: coverage-scoped test selection and forking from a warm checkpoint
exist precisely to remove it.

## Process note

The first attempt at this run produced zero mutations for four of six targets.
The cause was silent truncation — Nano emitting 23,000 characters of reasoning
and no content, with `finish_reason == "length"` and no error. This had already
been diagnosed, written up, and fixed in `proof.py` — and not applied to
`attacks.py`. The remedy now lives in `NemotronClient.complete()` where no caller
can forget it. Fix the layer, not the call site.

---

# Phase 1b — diff-scoped, 12 September 2026

Six real commits, mutations restricted to the lines each change touched.

| | Phase 1 (mature internals) | Phase 1b (changed lines) |
|---|---|---|
| mutations applied | 30 | 17 |
| killed | 27 | 14 |
| survivors | 3 (10%) | 3 (18%) |
| proven | 1 | **0** |

Combined: **1 of 6 survivors proven.** That is below the 40% line I said would
mean reconsidering the product, so it deserves a real diagnosis rather than a
shrug about sample size.

## The diagnosis: the model cannot see how to reach the code it must attack

Reading the three failed proof tests settles it. They are not incompetent — they
are well-formed tests of the wrong thing.

For `packaging`, where the mutation was `[:2]` to `[:3]` inside
`interpreter_abi`, Nemotron wrote a test for `pure_python_tags()` and asserted on
`py{major}{minor}`. Sensible code, adjacent function, completely unaffected by
the mutation. It failed rule 2 because it passes on both versions.

For `cachetools`, given an argument-transposition mutation, it wrote a test
passing those arguments *by keyword* — where order cannot matter. Again: passes
on both.

The pattern is the same every time. The mutated code is internal
(`_cmpkey`, `interpreter_abi`, `Cache.__setitem__`), the model is given the
module source and the mutation but no indication of how a caller actually
reaches that line, so it guesses at the public surface and guesses adjacent.

## Two concrete causes, one remedy

**The test file we show as context is chosen by size.** `run_prscoped.py` picks
the largest `test_*.py` in the repo, which for `packaging` is emphatically not
`test_tags.py`. The model's only worked examples are of unrelated code.

**Nothing tells the model which tests already execute the mutated line.** That
information exists — it is coverage — and it answers exactly the question the
model is failing to answer.

Both are fixed by the same mechanism. Run the existing suite under coverage,
identify the tests that execute the mutated line, and put *those* tests in the
prompt: "these existing tests already reach this line and none of them detect
the change; write one that does." That converts the hardest inference in the
task into given information.

## Coverage also fixes a claim we have been making loosely

A mutation on a line no test executes is not a blind spot. It is uncovered code,
which `coverage.py` already reports for free and which nobody needs an LLM to
find. The interesting finding — the one the product exists for — is a line that
*is* covered and still unconstrained.

We have not been separating those, which means some of what we would have
reported as blind spots was ordinary missing coverage dressed up. Gate 1 should
require that the mutated line be covered by at least one existing test, and the
report should say so.

## Honest status

The number is bad and the sample is small, and both are true at once. What keeps
this from being a verdict on the product is that the failures are specific and
mechanical rather than diffuse: in every case the model wrote a reasonable test
of code that the mutation does not touch, for a reason we can name and fix.

The next run decides it. With covering tests in the prompt, if the rate does not
move well above 40%, the problem is not instrumentation and the design needs
rethinking rather than tuning.
