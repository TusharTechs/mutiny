# 53 merged pull requests nobody chose for our benefit

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
regex, which is a heuristic; every title is recorded in the results so a reader
can disagree with any particular call.

## Results

    claim        n   divergence  preserved  no-probes  no-target  error
    fix         15            7          5          1          1      1
    no-claim     8            1          2          1          3      1
    feature      5            0          2          0          2      1
    other       25            3          8          4         10      0

    reached a verdict           28/53
    behaviour change found      11/28
    spend                       $0.26 for the whole corpus

## The number that matters most is 28/53

Just over half the pull requests produced an answer at all. The other 25 are not
"no behaviour change found" — they are *no result*, and folding them into a
detection rate would be dishonest:

- **16 no-target.** The changed lines were not inside a function MUTINY could
  locate and probe: module-level constants, class bodies, decorators, type
  annotations, or a file whose changed function could not be resolved in both
  revisions.
- **6 no-probes.** A target was found and no generated input could construct it.
  This is the stateful-construction weakness documented three times in
  `findings-differential.md` and confirmed on sqlalchemy in `findings-limits.md`.
- **3 error.** Installation or execution failed.

On the 28 that did produce an answer, and where the author said a behaviour
change was intended, MUTINY found one **7 times out of 12**.

## The case the benchmark was built to find

`jd/tenacity#679` — *"refactor: drop always-true truthiness checks and enable
truthy-bool"*. A pull request whose title is an explicit claim that behaviour is
preserved.

    RetryCallState(retry_object=Retrying(wait=None), ...)
      before  0.0
      after   TypeError: 'NoneType' object is not callable

The change removed `if self.wait:` before calling `self.wait(retry_state)`. The
author's reasoning is in the pull request body and it is sound: `wait` is typed
`WaitBaseT`, which implements neither `__bool__` nor `__len__`, so the guard can
only ever be true — *for a caller who respects the type*. MUTINY found the exact
boundary of that assumption: a caller passing `None` or `False` got `0.0` before
and gets a `TypeError` now.

Whether that matters is a judgement about whether untyped callers exist in the
wild, and this experiment does not make it. That is the intended division of
labour: the tool produces a reproducible fact, the reviewer decides what it is
worth. What it is not is a guess.

## A finding class worth naming: the arbitrary value that changed

Two of the eleven are true divergences of low value.

`Textualize/rich#3845` — *"Use faster generator for link IDs"* — changed link
IDs from `randint(0, 999999)` to a counter. MUTINY reports 24 divergences, all
of the form `'403958'` → `'14167048'`. `skorokithakis/shortuuid#103` —
*"Improve randomness"* — is the same shape.

These are real, reproducible and almost certainly uninteresting: the value was
arbitrary before and is arbitrary now. They are detectable at all because the
driver pins the random seed to make comparison possible, which turns "this value
is random" into "this value is deterministic and different".

Pinning the seed remains right — without it every such function diverges from
itself. But a change whose only divergence is in a value that was never
specified is a category the output should learn to label, and currently does
not.

## What this does and does not show

It shows the mechanism works on code chosen by other people for their own
reasons, at about half a cent per pull request, and that it can single out the
one function a fix touched from among several candidates.

It does not show that the coverage is adequate. 25 of 53 produced nothing, and
the largest bucket — changed lines that are not inside a probeable function — is
not addressed by any work done so far.
