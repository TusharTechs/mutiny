# Differential execution — findings

## The design

MUTINY asked Nemotron to produce the *evidence*: a test that mechanically proves
a specific bug. It managed that 13% of the time, at both 120B and 550B.

Differential execution inverts the burden. The model produces only *inputs*;
running two versions of the code on the same input produces the finding. The
distinction is structural rather than a matter of degree — a wrong assertion
manufactures a false finding, while a wrong input is rejected identically by both
versions and contributes nothing. **Bad inputs are wasted, never wrong.**

## A real behaviour change, found in the wild

`cachetools` commit `cc0cf229`, by the library's maintainer, titled
**"Minor style and readability improvements."**

```python
c = LFUCache(2); c["a"] = 1; c["b"] = 2; c["c"] = 3; (len(c), sorted(c.items()))
  before: (2, [('b', 2), ('c', 3)])   # evicted 'a'
  after:  (2, [('a', 1), ('c', 3)])   # evicted 'b'
```

The diff rewrote `return self.__touch(key)` as `self.__touch(key); return`, and
replaced a `try/except KeyError` with an `if/else`. Both read as cosmetic. They
changed which entry LFUCache evicts when usage counts tie.

Whether the maintainer considers the tie-breaking order part of the contract is
a fair question. What is not in question is that behaviour changed under a commit
message asserting it had not, and that a one-line reproduction exists. That is
the case the project is built for, and it was found without any knowledge of the
commit beyond its pre-change source.

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

Stability across repeated runs is 92%, up from swings that made five-case
comparisons unreadable. Cost is roughly $0.01 per commit examined. Detection rate
awaits a re-run with the module fix, since ten of nineteen behaviour-changing
cases could not produce a single input.
