# Where MUTINY stops working

Everything measured before this had been run on small, tidy libraries. The
question a judge will ask — *does it work on a real codebase?* — had not been
answered, so this walks up the size curve and records which stage fails first
and what each one costs.

`experiments/limits/run.py` times every stage separately and catches failures
per stage, so a repository that dies at `warm` still reports what `fetch` and
`archive` cost.

## Before

    repo                    files    MB  archive  probes  div   total  failed at
    pallets/flask              83   0.6     747K       0    0     48s  —
    psf/requests               37   0.4       0K       0    0     20s  —
    Textualize/rich           213   1.7   14727K      13    0     38s  —
    sqlalchemy/sqlalchemy     673  19.8    3684K       0    0    114s  —
    django/django            2932  18.5   10808K       0    0     62s  warm

One of five produced a result. Three reached probe generation and returned
nothing. Django never got off the ground.

## After

    repo                    files    MB  archive  tries  probes  div   total  failed at
    pallets/flask              83   0.6     273K      2       1    0     20s  —
    psf/requests               37   0.4     130K      3       9    0     17s  —
    Textualize/rich           213   1.7     445K      1      13    0     21s  —
    sqlalchemy/sqlalchemy     673  19.8    3661K      3       0    0     38s  probes
    django/django            2932  18.5    5364K      2      12    2     83s  —

Four of five. Django — 2932 files — now fetches, installs editable, generates
twelve probes, runs them across eight sandbox forks on both versions and returns
a verdict in 83 seconds for about half a cent.

## The four limits, and what each turned out to be

**1. The archive was mostly not code.** rich uploaded 14.7 MiB for 1.7 MB of
Python. Django's 10.8 MiB upload exceeded the SDK's ten-second
`transport_timeout` and failed outright. The archive now carries code, packaging
metadata and the repository root.

Two things went wrong while fixing it, both worth keeping on the record because
both failed *silently, three stages later*:

- Dropping `README.md` broke flask's editable install, because its `pyproject`
  names the readme as metadata. The symptom was an import error inside the
  driver, nowhere near the cause.
- Dropping directories called `locale` broke django, because `django/conf/locale`
  is a package the framework imports at startup. A directory name is not enough
  to judge by; the rule is now *prune it only if it holds no Python*.

Both now have tests asserting on archive contents rather than size.

**2. The timeout could not be raised after construction.** `ContreeConfig` is a
plain dataclass and `client.config.transport_timeout = 180` assigns cleanly — and
changes nothing, because the HTTP client is already built. It has to be passed to
the constructor. This is in `docs/feedback.md` for Nebius.

**3. The busiest function is often the least reachable one.** `_busiest_function`
picked `find_best_app`, `RequestEncodingMixin._encode_files`,
`InstanceState._modified_event` — the densest branching in a framework is
usually behind the most setup. Target selection now returns a ranked list and
walks down it, reusing one warmed sandbox: flask succeeded on its second
candidate, requests on its third, django on its second.

**4. Some frameworks cannot be imported until configured.** Django raises before
a single probe runs; no input the generator writes can get past a barrier at
import time. The driver now applies a documented bootstrap for django and adds
`src/` and `lib/` to the path for projects whose editable install fails —
sqlalchemy's needs a C toolchain and does not get one, but the package is still
in the tree.

This is deliberately a short list of known bootstraps rather than a guess at what
an arbitrary project needs.

## What is still a limit

**sqlalchemy.** It installs, it imports, the probe stage runs — and produces
nothing usable across three candidates. `InstanceState._modified_event` and
`ClauseAdapter.replace` need real ORM objects with real sessions behind them.

This is the same weakness recorded three times already in
`findings-differential.md`: single call expressions test functions, and stateful
machinery needs scenarios. It is not an infrastructure problem and raising a
timeout will not touch it. The honest statement of MUTINY's boundary is that it
verifies code reachable from a constructible input, which covers libraries,
utilities and most application code, and does not yet cover ORM internals.
