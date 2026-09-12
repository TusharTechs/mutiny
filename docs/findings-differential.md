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
| behaviour changes detected | **4/14 = 29%** |
| preserving commits flagged | **0/5** |
| runs agreeing across repeats | **19/19 = 100%** |
| cases yielding no inputs | 5/24 (excluded — they measure nothing) |
| cost | ~$0.01 per commit |

29% is the honest figure, after removing artifacts that had inflated it to 43%.
Zero false positives is the number worth defending: across five genuinely
behaviour-preserving commits, the harness stayed silent every time.

The confirmed detections are real and specific — semver's `bump_prerelease`
producing `'.0'` where it produced `''`, cachetools' `RRCache.popitem` returning
a different entry, and packaging's marker serialisation turning `'"""'` into
`'\'"\''`.
