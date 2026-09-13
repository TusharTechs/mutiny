# 49 merged pull requests nobody chose for our benefit

Every earlier number answers a narrower question: can MUTINY detect a behaviour
change that was deliberately introduced — by a known fix commit, or by asking a
model to rewrite a function. That establishes the mechanism works. It does not
establish that anyone needs it.

This runs MUTINY over recently merged pull requests from twelve widely used
Python libraries, taken in the order GitHub returned them, and splits the
results by **what each pull request claimed about itself**:

    fix       the author says behaviour changes. Finding it is a sensitivity check.
    no-claim  refactor, cleanup, chore, docs, perf — the author is asserting
              behaviour is preserved.
    feature   new behaviour, expected to diverge.
    other     no convention in the title to go on.

`experiments/prs/run.py`. The claim is classified from the title and labels by
regex, which is a heuristic; every title is in the results so a reader can
disagree with any particular call.

## Results

    claim        n  divergence preserved no-probes no-target new-code nothing- error
    fix         15          6         7         1        0        0        0      1
    no-claim     7          2         3         0        0        0        1      1
    feature      5          0         3         0        1        0        0      1
    other       22          4        11         3        1        3        0      0

    had behaviour to check      45/49
    reached a verdict           36/45
    behaviour change found      12/36
    spend                       $0.39 for the corpus

Verdict coverage over the three rounds of work this corpus drove:

    28/53    at first run
    33/45    after target selection reached changes outside a function
    36/45    after the probe generator was shown the repository's own tests

The two "no-claim" rows are **one distinct change**. `jd/tenacity#680` is
stacked on `#679` — its branch contains both commits and they merged 51 seconds
apart — so the merge base legitimately predates both and the same removed guard
is reported against each. That is correct: a reviewer opening #680 sees #679's
commit in the branch too. It is not two independent findings, and counting it as
two would be wrong.

Four of the 49 had no behaviour to check at all: three added only new code, and
one changed no library source. Those are not failures to find anything — they
are pull requests with nothing for a differential tool to compare, and counting
them against the tool would be measuring the shape of the corpus.

## The case the benchmark was built to find

`jd/tenacity#679` — *"refactor: drop always-true truthiness checks and enable
truthy-bool"*. A title that is an explicit claim that behaviour is preserved.

    RetryCallState(retry_object=Retrying(wait=None), ...)
      before  0.0
      after   TypeError: 'NoneType' object is not callable

The change removed `if self.wait:` before calling `self.wait(retry_state)`. The
author's reasoning is in the pull request body and it is sound: `wait` is typed
`WaitBaseT`, which implements neither `__bool__` nor `__len__`, so the guard can
only ever be true — *for a caller who respects the type*. MUTINY found the exact
boundary of that assumption.

Whether it matters is a judgement about whether untyped callers exist in the
wild, and this experiment does not make it. That is the intended division of
labour: the tool produces a reproducible fact, the reviewer decides what it is
worth. What it is not is a guess.

## Two false positives this corpus found in MUTINY itself

Running against code nobody selected is a better test of the tool than of the
code. Both of these were reported as confident findings, and neither was one.

**An identity that is not written "at 0x...".** `jd/tenacity#682` did nothing
but add `@override` decorators and reported eight divergences. Every one was a
memory address: tenacity's repr is `<RetryCallState 140737342193440: ...>`, with
the id straight after the class name, and the address filter only matched the
default `object at 0x...` shape. The decorators shifted the allocation order and
every address moved.

`confirm()` could not have caught it, and this is the interesting part: two
fresh processes allocate identically, so the value looked perfectly stable
*within* each version and different *between* them. **Stability across runs is
not the same property as being a value**, and only canonicalisation can tell
them apart.

The tell was already on the page. Nemotron's explanation read *"the only
observable difference is the object's address"* directly beneath a verdict
saying behaviour had changed.

**A coin that landed the same way twice.** `shortuuid#103` — *"Improve
randomness"* — reported `random(length=1)` returning `'4'` before and `'a'`
after. Confirmation ran the unchanged version twice and kept probes that agreed
with themselves; one character drawn from an alphabet of 57 has a 1-in-57 chance
of agreeing by luck, and it took that chance.

Sandbox forks sharing entropy was the other candidate explanation and was ruled
out by measurement: forked from one checkpoint, `os.urandom` and
`secrets.choice` both still vary. Confirmation now takes three samples — 1 in
3249 — and checks both versions rather than only the unchanged one. The finding
is now correctly reported as preserved.

## A finding class worth naming: the arbitrary value that changed

`Textualize/rich#3845` — *"Use faster generator for link IDs"* — changed link
IDs from `randint(0, 999999)` to a counter, and MUTINY reports 24 divergences of
the form `'794772'` → `'14167049'`.

This one is real and survives confirmation legitimately: both implementations
are deterministic under a pinned seed, so it is not a flake. It is simply not
interesting. The value was arbitrary before and is arbitrary now.

Pinning the seed remains right — without it every such function diverges from
itself. But a change whose only divergence is in a value that was never
specified is a category the output should learn to label, and currently does
not.

## What this does and does not show

It shows the mechanism works on code chosen by other people for their own
reasons, at about half a cent per pull request, and that it can single out the
one function a fix touched from among several candidates.

It does not show the coverage is adequate. A third of the pull requests with
real behaviour to check still produce no verdict, and the largest remaining
cause is a target no generated input can construct — the stateful-construction
weakness documented in `findings-differential.md` and confirmed on sqlalchemy in
`findings-limits.md`.
