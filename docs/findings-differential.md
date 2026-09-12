# Differential execution — findings

## The design

MUTINY asked Nemotron to produce the *evidence*: a test that mechanically proves
a specific bug. It managed that 13% of the time, at both 120B and 550B.

Differential execution inverts the burden. The model produces only *inputs*;
running two versions of the code on the same input produces the finding. The
distinction is structural rather than a matter of degree — a wrong assertion
manufactures a false finding, while a wrong input is rejected identically by both
versions and contributes nothing. **Bad inputs are wasted, never wrong.**

## A retracted finding, and why it matters

This document previously reported that `cachetools` commit `cc0cf229` — titled
"Minor style and readability improvements" — silently changed which entry
`LFUCache` evicts. **That was wrong.** The commit is exactly what it claims.

The apparent divergence was two processes running with different random hash
seeds. LFU tie-breaking depends on dict iteration order, which depends on string
hashing, which Python randomises per process unless `PYTHONHASHSEED` is set.
Pinning it settles the question:

| PYTHONHASHSEED | before | after | |
|---|---|---|---|
| 0 | `(2, [('b',2), ('c',3)])` | `(2, [('b',2), ('c',3)])` | same |
| 1 | `(2, [('b',2), ('c',3)])` | `(2, [('b',2), ('c',3)])` | same |
| 12345 | `(2, [('a',1), ('c',3)])` | `(2, [('a',1), ('c',3)])` | same |

The output varies with the seed and not with the commit.

It is worth being blunt about how close this came to being published. It was
reported internally as the project's headline result — a maintainer shipping a
silent behaviour change under a cosmetic message. Putting that in a submission,
or worse in an upstream issue, would have been a public and unretractable
accusation about someone's work, based on a bug in our harness.

Three classes of artifact have now been found and fixed, all of which produced
confident, specific, entirely false findings:

- **memory addresses** in the default `repr`, which differ every process, so any
  object without a custom `__repr__` diverged by construction;
- **set and dict iteration order**, which made `frozenset({a, b})` and
  `frozenset({b, a})` look like different values;
- **hash seed randomisation**, which changes the behaviour of anything whose
  logic depends on dict ordering.

The through-line: a differential harness compares *observations*, and an
observation that varies for reasons the caller cannot control is not evidence.
Every source of nondeterminism has to be pinned or normalised before a
divergence means anything. The three found so far are unlikely to be the last,
so the standard for any future finding is that it survives a pinned seed and a
canonicalised comparison, and reproduces by hand.

## Model selection is decided by task shape, not size

Measured on an identical prompt asking for 45 call expressions:

| model | inputs | tokens | reasoning | time |
|---|---:|---:|---:|---:|
| Nano | 44 | 9,846 | 26,300 ch | 33.9 s |
| Lightning | 1 | 14,000 | 0 | 56.0 s |
| **Super** | **42** | **4,520** | **0** | **10.7 s** |

Nano deliberates for 26,000 characters about how to write call expressions and
often exhausts its allowance before writing one. Super does no reasoning at all
on this task and answers directly.

This is the opposite of the proof-test result, where Super was the *worst*
choice: starved of tokens it returned empty content and empty reasoning, while
Nano and Ultra at least returned something to widen toward. The same model is
best at one task and worst at the other. Parameter count predicts neither.

## Failures found by validating, and what each cost

Every one of these was silent — the run completed and reported a number.

**The expression filter rejected every dunder attribute**, to block
introspection. It also blocked `Cache().__setitem__(0, 0)`, the one expression
that exercises the method under test. Two of six cases had every input discarded.

**Single expressions cannot express stateful behaviour.** Filling a cache and
then replacing an entry with a larger one is three operations. Inputs are now
short statement sequences ending in an expression.

**A method on an abstract base is often unobservable through the base.**
`Cache.__setitem__` over-evicted when growing an entry, but `Cache.popitem`
raises `NotImplementedError`, so nothing is ever evicted and the change cannot
appear. It shows only through `LRUCache`. Concrete subclasses are now derived
from the module and offered as receivers.

**The module name was hardcoded per repository.** `packaging`'s symbols live in
`packaging._ranges`, `packaging.version`, `packaging.tags` — importing the
top-level package meant every expression raised `NameError`. All eight packaging
cases reported zero usable inputs rather than an error, and depressed the
detection rate from a real number to 16%.

## Status

Measured across 24 real commits, each run twice, with ground truth taken from
commit messages:

| | |
|---|---|
| behaviour changes detected | **8/13 = 62%** |
| preserving commits flagged | **0/3** |
| runs agreeing across repeats | **15/16 = 94%** |
| cases yielding no inputs | 8/24 (excluded — they measure nothing) |
| cost | ~$0.01 per commit |

62%, up from 29%, entirely from showing the generator the change under review.
The four commits that recovered were all validation or error-path fixes, which a
generator shown only the working source has no reason to probe:

```
next_version("prerelease") on 1.0.0-beta.2   '1.0.0-beta.3' -> '1.0.0-rc.1'
bump_build("")                               'build.1'      -> '1'
_process_summary("\r")                       returned "\r"   -> raises InvalidMetadata
parse_tag("3py-none-any")                    accepted       -> raises InvalidTag
```

Zero false positives has held through every revision, which is the property that
decides whether anyone leaves the tool switched on.
Zero false positives is the number worth defending: across five genuinely
behaviour-preserving commits, the harness stayed silent every time.

The confirmed detections are real and specific — semver's `bump_prerelease`
producing `'.0'` where it produced `''`, cachetools' `RRCache.popitem` returning
a different entry, and packaging's marker serialisation turning `'"""'` into
`'\'"\''`.

---

# Agent refactor safety — the product's real question

Nemotron refactors a real function; the repository's tests judge it; the
differential is run regardless, so the refactors the tests *rejected* serve as
ground truth for whether we would have been a safety net without them.

Twenty-one functions across semver, cachetools and packaging.

| | |
|---|---|
| refactors produced | 18/21 |
| broken — tests rejected them | **3/18 = 17%** |
| of the known-broken, differential also caught | **1/3 = 33%** |
| of the 13 tests accepted, behaviour changed | **0/13** |
| cost | $0.08 for the set |

## Three things this establishes

**The problem is real and we reproduced it.** 17% of refactors from a frontier
open model were functionally incorrect, against a published range of 19-35%.

**False positives remain at zero.** Thirteen correct refactors, thirteen
silences. Across every experiment today — five behaviour-preserving commits and
now thirteen good refactors — the harness has never once cried wolf. That is the
property that decides whether a team leaves a tool switched on, and it is the
number this project can defend.

**Detection is partial and the gap has a shape.** One of three. Both misses were
`Cache.__setitem__` and `LRUCache.popitem` — stateful methods whose behaviour
only emerges from a sequence of operations under capacity pressure.

## The weakness is the same one, three times over

It has now appeared in three unrelated experiments: cachetools' over-eviction
fix needed `LRUCache` rather than the abstract base and a three-step sequence;
`Cache.__setitem__` is still missed; `LRUCache.popitem` is still missed.

Single call expressions test functions. Stateful classes need *scenarios*:
construct with a specific capacity and sizing function, fill past that capacity,
then act. The generator can write those — it did for the hand-checked case — but
it does not reliably reach for them.

The existing tests are the obvious source. cachetools' own suite is full of
exactly these sequences, and coverage already tells us which tests reach the
changed line. Showing those sequences as templates, rather than as generic
examples of construction, is the next thing to try.

## Honest position

Detection is 62% on human `fix:` commits and 33% on known-broken agent
refactors. Neither is a finished product. What is solid is the direction and the
precision: the mechanism produces concrete, reproducible, one-line witnesses, it
costs about a cent per function, and it has never produced a false alarm.
