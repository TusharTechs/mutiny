#!/usr/bin/env python
"""Does working out the construction first rescue the targets that produced nothing?

These functions have defeated probe generation from the beginning: the receiver
needs a paragraph of setup, the generator is asked to invent it inside a single
expression, and it cannot. Measured before and after, same model, same sandbox.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny.construct import find_recipe
from mutiny.fetch import cleanup, fetch
from mutiny.inputs import generate_validated, is_stateful, receiver_candidates
from mutiny.models import NemotronClient
from mutiny.sandbox import SandboxExecutor
from mutiny.session import _locate
from mutiny.source import focused_module

CASES = [
    ("https://github.com/sqlalchemy/sqlalchemy", "ClauseAdapter.replace"),
    ("https://github.com/tkem/cachetools", "Cache.__setitem__"),
    ("https://github.com/jd/tenacity", "BaseRetrying._run_wait"),
    ("https://github.com/dbader/schedule", "Job._schedule_next_run"),
]


def main() -> int:
    client = NemotronClient(cap_usd=10.0)
    opening = client.ledger.total_usd
    rows = []

    by_repo: dict[str, list[str]] = {}
    for url, qualname in CASES:
        by_repo.setdefault(url, []).append(qualname)

    for url, targets in by_repo.items():
        print(f"\n{url}", flush=True)
        source = fetch(url)
        try:
            executor = SandboxExecutor()
            executor.warm(source.path)
            print(f"  {executor.install_mode}", flush=True)

            for qualname in targets:
                located = _locate(source.path, qualname)
                if located is None:
                    print(f"  {qualname}: not found", flush=True)
                    continue
                path, module = located
                text = path.read_text(encoding="utf-8")
                focused = focused_module(text, qualname)
                owner = qualname.rsplit(".", 2)[-2]
                probe = lambda exprs: executor.observe(module, exprs)

                started = time.monotonic()
                before, _ = generate_validated(
                    client, module, qualname, focused, probe=probe, n=20,
                    subclasses=receiver_candidates(text, qualname),
                    stateful=is_stateful(text, qualname))
                plain = time.monotonic() - started

                started = time.monotonic()
                recipe = find_recipe(client, module=module, owner=owner, source=text,
                                     repo=source.path, probe=probe, qualname=qualname)
                after = []
                if recipe:
                    after, _ = generate_validated(
                        client, module, qualname, focused, probe=probe, n=20,
                        subclasses=receiver_candidates(text, qualname),
                        stateful=is_stateful(text, qualname), recipe=recipe.setup)
                guided = time.monotonic() - started

                rows.append((source.slug, qualname, len(before), bool(recipe),
                             recipe.attempts if recipe else 0,
                             ", ".join(recipe.evidence) if recipe else "", len(after)))
                print(f"  {qualname}", flush=True)
                print(f"    without a recipe   {len(before):>3} probes  {plain:>5.1f}s",
                      flush=True)
                if recipe:
                    seen = f" after reading the {' and '.join(recipe.evidence)}" \
                        if recipe.evidence else ""
                    print(f"    built it on attempt {recipe.attempts}{seen}", flush=True)
                    for line in recipe.setup.splitlines()[-3:]:
                        print(f"      {line[:86]}", flush=True)
                else:
                    print("    could not build the receiver", flush=True)
                print(f"    with a recipe      {len(after):>3} probes  {guided:>5.1f}s",
                      flush=True)
        finally:
            cleanup(source)

    print(f"\n\n{'=' * 78}\nCONSTRUCTION LOOP\n{'=' * 78}")
    print(f"{'target':<44}{'before':>8}{'built':>7}{'try':>5}{'after':>7}")
    print("-" * 78)
    for slug, qualname, before, built, attempts, evidence, after in rows:
        name = f"{slug.split('/')[-1]} {qualname}"[:43]
        print(f"{name:<44}{before:>8}{'yes' if built else 'no':>7}"
              f"{attempts or '-':>5}{after:>7}")
    print("-" * 78)
    rescued = sum(1 for r in rows if r[2] == 0 and r[6] > 0)
    print(f"  rescued from zero: {rescued}")
    print(f"  spend ${client.ledger.total_usd - opening:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
