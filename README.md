# MUTINY

**Did that refactor actually preserve behaviour?**

An agent rewrites your function. The tests pass. MUTINY runs both versions on
hundreds of generated inputs and shows you the exact input where they disagree —
or tells you there isn't one.

```
c = Cache(maxsize=2); c["a"] = 1; sorted(c.items())
    before: [('a', 1)]
    after:  AttributeError: '_DefaultSize' object has no attribute 'get'
```

That is a real refactor, produced by Nemotron 3 Super, of `cachetools.Cache.__setitem__`.
It reads as a tidy-up. It breaks the library for every caller who does not pass
an explicit `getsizeof`.

## Why this exists

Published work finds **19–35% of LLM-generated refactorings are functionally
incorrect, and over 21% of those errors pass the project's existing tests**. We
reproduced the first half independently: of 18 refactors Nemotron produced for
real functions in `semver`, `cachetools` and `packaging`, **17% were rejected by
the repositories' own test suites**.

Teams are now merging agent-written changes at a rate no review process was
designed for, and the test suite is the only gate. When the change is subtle —
an edge case, a default argument, an eviction order — the suite is not enough.

## How it works

The model's only job is to produce **inputs**. Execution produces the evidence.

```
  function under review
          │
          ├── Nemotron generates call expressions and stateful scenarios
          │   (informed by the diff, by tests that already cover the line,
          │    and by the concrete subclasses that can stand in as receivers)
          │
          ├── run every input against the ORIGINAL
          ├── run every input against the REWRITE
          │
          └── compare outcomes ─── divergence? here is the input that proves it
```

That division of labour is the whole design, and it is why this works where our
first attempt did not. **A wrong assertion manufactures a false finding. A wrong
input is rejected identically by both versions and simply contributes nothing.**
Bad inputs are wasted, never wrong — so the model only has to be *productive*,
not *correct*.

An earlier version of this project asked Nemotron to write a test proving a
specific bug. It managed that 13% of the time at 120B and 13% at 550B. Inverting
the burden moved the same task to 62–67%.

## Results

Measured on real code, not fixtures. Every number is reproducible from
`experiments/`.

**Agent refactor safety** — Nemotron refactors a function, the repository's own
tests judge it, and the differential runs regardless:

| | |
|---|---|
| refactors produced | 18 / 21 functions |
| functionally broken (tests rejected them) | **17%** |
| of those, differential also caught | **67%** |
| of the 14 tests accepted, behaviour changed | **0** |
| cost | ~$0.01 per function |

**Detection on real bug-fix commits** — 24 commits across three libraries, each
run twice, ground truth taken from commit messages:

| | |
|---|---|
| behaviour changes detected | **62%** |
| behaviour-preserving commits falsely flagged | **0** |
| runs agreeing across repeats | 94% |

The zero is the number worth defending. Across 14 correct refactors and 5
behaviour-preserving commits, MUTINY has never raised a false alarm.

Getting there meant finding three separate ways a comparison can lie: memory
addresses in the default `repr`, which differ every process; set and dict
iteration order, which made identical collections look different; and hash-seed
randomisation, which changes the behaviour of anything whose logic touches dict
ordering. Each produced confident, specific, entirely false findings before it
was fixed. A verification tool that cries wolf gets switched off in a week.

## How NVIDIA Nemotron is used

Nemotron does two jobs, both load-bearing:

1. **It writes the code under review.** `mutiny/refactor.py` asks
   `nemotron-3-super-120b-a12b` to refactor a real function for readability while
   preserving behaviour. This is the change MUTINY then verifies.
2. **It generates the probes.** `mutiny/inputs.py` asks it for call expressions
   and stateful scenarios that reach the changed lines, steered by the diff, by
   the tests that already cover those lines, and by the concrete subclasses
   available as receivers.

### Choosing a model is about task shape, not parameter count

We measured all four on an identical prompt asking for 45 call expressions:

| model | usable inputs | completion tokens | reasoning emitted | time |
|---|---:|---:|---:|---:|
| Nemotron 3 Nano 30B-A3B | 44 | 9,846 | 26,300 chars | 33.9 s |
| Nemotron 3.5 Lightning | 1 | 14,000 | 0 | 56.0 s |
| **Nemotron 3 Super 120B-A12B** | **42** | **4,520** | **0** | **10.7 s** |
| Nemotron 3 Ultra 550B-A55B | — | — | — | — |

Super emits *no reasoning at all* on this task and answers directly, in a third
of Nano's time for half the tokens. Nano deliberates for 26,000 characters about
how to write call expressions and frequently exhausts its allowance before
writing one.

The reverse held for the harder task. On writing a test that mechanically proves
a specific bug, Ultra scored **identically to Super — 2/15 each**, and Super
failed *worse* than the smaller models when starved of tokens. Bigger was not
better; the shape of the work decided the model.

## Where Token Factory accelerated the work

- **One OpenAI-compatible endpoint for four models.** Swapping Nano for Super
  for Ultra is a string change, which is what made the comparison table above
  cheap enough to actually run rather than assume.
- **`mutiny/models.py`** wraps it with a spend cap, a persistent token ledger and
  content-hash response caching. Re-running an experiment costs nothing, which
  turned a 2.5-hour benchmark into a 20-minute one and made iterating on the
  measurement affordable. Total spend across every experiment in this repository
  is under $3.
- **Reasoning-aware retry.** Nemotron 3 bills its reasoning trace against
  `max_tokens`, so a generous-looking allowance can be spent entirely on
  thinking. The client detects that and widens, except for one signature where
  widening provably never helps (see `docs/feedback.md`).

## Nebius Sandboxes

`mutiny/sandbox.py` implements the executor against the ConTree SDK, behind the
same interface as the local one.

The fit is unusually good: `image.run()` returns a *new* image rather than
mutating the old one, so running N probes against one warm image is **N forks
from a single checkpoint, isolated by construction**. That is exactly this
workload's shape — one expensive setup (clone, install, warm the interpreter),
then hundreds of short independent executions that must not see each other's
state. Locally we pay setup once and then serialise; on Sandboxes the batches run
concurrently, up to the documented ceiling of 50.

Measured against the live service, on `python-semver` installed from source:

| | |
|---|---|
| warm checkpoint (upload, extract, `pip install -e .`) | 9.8 s, paid once |
| 40 forks, run sequentially | 50.4 s |
| **40 forks, run concurrently** | **3.7 s — 0.09 s each** |
| local subprocess, same probes | 0.10 s each |

Forks are genuinely isolated: each inherits everything the checkpoint wrote, sees
nothing a sibling wrote, and leaves the checkpoint unchanged. Sandbox and local
execution agreed on 23 of 23 observations.

The honest comparison is that a *single* sandbox fork is slower than a local
subprocess — 1.2 s against 0.1 s, being network and a microVM. Concurrency is the
entire reason to be here, and it inverts that: at 40 forks the per-fork cost is
below local, and the work is spread across forty isolated machines rather than
competing on one. That matters twice over, because the code being executed was
written by a model and should not run on a developer's laptop at all.

## Try it

```bash
mutiny verify path/to/repo Cache.__setitem__
```

Nemotron rewrites the function, both versions run on generated inputs inside
forks of one warm Sandboxes checkpoint, and you get either the input where they
disagree or a clean bill:

```
cachetools  src/cachetools/__init__.py  Cache.__setitem__  (cachetools)

rewriting with nemotron-3-super-120b-a12b...
    -        diffsize = size - self.__size[key]
    +        old_size = self.__size.get(key, 0)
    +        needed = size - old_size
    ...
warming a sandbox checkpoint...
  ready in 8.8s, 44 KiB uploaded
generating probes...
  10 probes
running both versions...

Behaviour changed.  5 of 10 probes disagree.

  c = LRUCache(3); c["a"] = "x"; c["b"] = "yy"; (sorted(c.items()), c.currsize, len(c))
    before: ([('a', 'x'), ('b', 'yy')], 2, 2)
    after:  AttributeError: '_DefaultSize' object has no attribute 'get'

36.2s, $0.0065
```

`--local` runs the probes on this machine instead, which is faster for a single
pass and appropriate only for code you trust.

### Reviewing a change that already exists

```bash
mutiny verify-diff path/to/repo --base main --head my-branch
```

Finds the functions a branch touched and checks each one, comparing the two
revisions. Nothing is checked out — `git archive` and `git show` read straight
out of the object store, so your working copy is untouched and CI jobs sharing a
clone do not fight each other. One checkpoint is warmed at the merge base, and
each fork has the head version of the changed files laid over the top.

```
python-semver  d8813b67^..d8813b67
2 changed function(s) to check, 1 skipped

Version.next_version  src/semver/version.py
  behaviour changed — 1 of 14 probes disagree
    str(Version(1,2,3, prerelease="rc").next_version("prerelease"))
      before: '1.2.3-rc'
      after:  '1.2.3-rc.0'

2 of 2 changed functions behave differently.
28.4s, $0.0070
```

## Point it at your own code

```bash
mutiny verify-url https://github.com/owner/repo/pull/123
mutiny verify-url https://github.com/owner/repo
```

A pull request is checked against its merge base, so other people's work on main
is not attributed to the change under review. A repository with no change to
review has its most heavily branched function rewritten, and that rewrite
checked.

Only public GitHub repositories, the clone is size-capped, and nothing is
installed or executed on the machine running MUTINY — that happens inside a
sandbox, which is the reason the sandbox is there.

## The web interface

```bash
uv pip install -e ".[web]" --python .venv/bin/python
.venv/bin/python -m uvicorn app.server:app --port 8000
```

Four pre-seeded examples — a rewrite that breaks, one that does not, a real
bug-fix commit reviewed as a pull request, and a pure style commit that should
stay silent. Forks appear as they run, so the concurrency is visible rather than
asserted, and each finding shows the plain-English summary above the input that
proves it.

Repositories are pre-seeded deliberately: a public endpoint that clones and
installs an arbitrary URL on request is an obvious way to be abused, and the
demonstration does not need it.

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv/bin/python
cp .env.example .env          # add NEBIUS_API_KEY and NEBIUS_PROJECT_ID
.venv/bin/python scripts/doctor.py
```

`doctor.py` verifies connectivity, credentials, model availability and spend.
Run it first whenever anything fails.

### Reproducing the results

The benchmarks clone three real repositories and run against their history:

```bash
.venv/bin/python experiments/refactor/run.py     # agent refactor safety
.venv/bin/python experiments/diff/scaled.py 8    # detection on real fix commits
.venv/bin/python -m pytest tests/ -q             # 52 tests
```

## Architecture

| module | responsibility |
|---|---|
| `mutiny/session.py` | the verification loop, yielded as events |
| `mutiny/cli.py` | terminal rendering of that stream |
| `mutiny/review.py` | resolving a branch or commit into changed functions |
| `mutiny/explain.py` | Nemotron describes the change, grounded in the witnesses |
| `mutiny/fetch.py` | a GitHub URL becomes a working copy, and a PR its merge base |
| `app/` | the web interface — the same events over server-sent events |
| `mutiny/refactor.py` | Nemotron rewrites a function; the rewrite is applied in place |
| `mutiny/inputs.py` | Nemotron generates probes; execution-validated before use |
| `mutiny/differential.py` | runs both versions, canonicalises observations, compares |
| `mutiny/coverage.py` | which tests execute which line — steers probes and test selection |
| `mutiny/diff.py` | changed lines, source-vs-test filtering, enclosing functions |
| `mutiny/source.py` | locating a function by qualified name, and showing just enough of it |
| `mutiny/models.py` | Token Factory client: spend cap, ledger, caching, retry |
| `mutiny/sandbox.py` | the same execution, forked from a warm Sandboxes checkpoint |
| `mutiny/tls.py` | reactive certificate-trust repair for inspecting proxies |

An observation records the value's **type**, a canonicalised `repr`, and for
failures the exception type and normalised message. Memory addresses are
stripped, unordered containers are sorted, and the hash seed is pinned — because
an observation that varies for reasons the caller cannot control is not
evidence. Each of those was added after it produced a confident, specific,
entirely false finding.

## What it does not do

- **Side effects are invisible.** A function that mutates its argument or writes
  a file and returns `None` gives nothing to compare.
- **Exception chaining is not observed.** One known miss is a refactor that
  changed `__context__` while preserving the raised type and message.
- **Pinning the hash seed hides seed-dependent behaviour.** Necessary to compare
  two runs at all, and a real trade-off: it cost us a finding we had to retract.
- **Rich domain objects are hard to probe.** Everything found so far takes
  primitives or simply-constructible values. A function wanting three populated
  domain objects and a timezone-aware datetime defeats the generator, and yield
  collapses to zero.
- **Unreachable code stays unreached.** A branch requiring a particular OS, a
  network condition or a race has no call expression that gets to it.

`docs/findings-differential.md` records the measurements and the retraction in
full, including a finding we reported internally as the headline result and then
withdrew when a pinned hash seed showed it was an artifact of our own harness.

## Licence

Apache-2.0.
