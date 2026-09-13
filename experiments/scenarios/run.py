#!/usr/bin/env python
"""Does showing the repository's own tests help the probe generator?

The stateful-construction weakness is the most persistent failure on record: it
defeated Cache.__setitem__, every ORM function in sqlalchemy, and a third of the
pull requests in the corpus that had real behaviour to check. The hypothesis is
that the repository already contains the answer, in the tests that construct
these objects for their own purposes.

A/B on the functions known to fail. Same target, same model, same sandbox; the
only difference is whether the mined tests are in the prompt.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny.fetch import cleanup, fetch
from mutiny.inputs import generate_validated, is_stateful, receiver_candidates
from mutiny.models import NemotronClient
from mutiny.sandbox import SandboxExecutor
from mutiny.scenarios import construction_examples
from mutiny.session import _locate
from mutiny.source import focused_module

CASES = [
    ("https://github.com/tkem/cachetools", "Cache.__setitem__"),
    ("https://github.com/tkem/cachetools", "LRUCache.popitem"),
    ("https://github.com/dbader/schedule", "Job.__repr__"),
    ("https://github.com/dbader/schedule", "Job.run"),
    ("https://github.com/sqlalchemy/sqlalchemy", "ClauseAdapter.replace"),
    ("https://github.com/jd/tenacity", "BaseRetrying.__repr__"),
]


def main() -> int:
    client = NemotronClient(cap_usd=8.0)
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
            print(f"  sandbox: {executor.install_mode}", flush=True)

            for qualname in targets:
                located = _locate(source.path, qualname)
                if located is None:
                    print(f"  {qualname}: not found", flush=True)
                    continue
                path, module = located
                text = path.read_text(encoding="utf-8")
                focused = focused_module(text, qualname)
                examples = construction_examples(
                    source.path, qualname, module_source=text)

                counts = {}
                for label, tests in (("without", None), ("with", examples)):
                    started = time.monotonic()
                    try:
                        probes, _ = generate_validated(
                            client, module, qualname, focused,
                            probe=lambda e: executor.observe(module, e), n=24,
                            subclasses=receiver_candidates(text, qualname),
                            stateful=is_stateful(text, qualname),
                            covering_tests=tests,
                        )
                    except Exception as exc:  # noqa: BLE001
                        probes = []
                        print(f"    {label}: {type(exc).__name__}: {exc}"[:110], flush=True)
                    counts[label] = len(probes)
                    print(f"    {label:<8} {len(probes):>3} probes  "
                          f"{time.monotonic() - started:>5.1f}s", flush=True)

                rows.append((source.slug, qualname, len(examples),
                             counts.get("without", 0), counts.get("with", 0)))
        finally:
            cleanup(source)

    print(f"\n\n{'=' * 74}\nSTATEFUL CONSTRUCTION\n{'=' * 74}")
    print(f"{'target':<44}{'tests':>7}{'without':>9}{'with':>7}")
    print("-" * 74)
    for slug, qualname, examples, without, with_ in rows:
        print(f"{(slug.split('/')[-1] + ' ' + qualname)[:43]:<44}"
              f"{examples:>7}{without:>9}{with_:>7}")
    print("-" * 74)
    helped = sum(1 for *_, without, with_ in rows if with_ > without)
    unblocked = sum(1 for *_, without, with_ in rows if without == 0 and with_ > 0)
    print(f"  more probes with examples   {helped}/{len(rows)}")
    print(f"  zero -> some                {unblocked}")
    print(f"  spend ${client.ledger.total_usd - opening:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
