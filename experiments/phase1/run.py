#!/usr/bin/env python
"""Phase 1 — the go/no-go.

Does the whole loop work on code we did not write? For each target function:
synthesise attacks with Nano, filter them through Gate 1, run the real suite to
find survivors, then ask Super for a proof test and let Gate 2 decide.

The number that matters is the last column: of the mutations that survive a real
test suite, how many can we prove, and in how many attempts.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import gate1
from mutiny.attacks import generate_mutations
from mutiny.models import BudgetExceeded, NemotronClient
from mutiny.proof import generate_proof_test
from mutiny.runner import PASSED, run_pytest

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT / "checkouts"

# (repo, module, qualified function, import name, existing tests, suite selector)
TARGETS = [
    ("python-semver", "src/semver/version.py", "Version.compare",
     "semver", "tests/test_semver_compare.py", "tests/"),
    ("python-semver", "src/semver/version.py", "Version.next_version",
     "semver", "tests/test_bump.py", "tests/"),
    ("cachetools", "src/cachetools/__init__.py", "TTLCache.expire",
     "cachetools", "tests/test_ttl.py", "tests/"),
    ("cachetools", "src/cachetools/__init__.py", "LRUCache.popitem",
     "cachetools", "tests/test_lru.py", "tests/"),
    ("packaging", "src/packaging/version.py", "_cmpkey",
     "packaging.version", "tests/test_version.py", "tests/test_version.py"),
    ("packaging", "src/packaging/version.py", "_parse_letter_version",
     "packaging.version", "tests/test_version.py", "tests/test_version.py"),
]

N_MUTATIONS = 6
MAX_PROOF_ATTEMPTS = 3


@dataclass
class TargetResult:
    repo: str
    function: str
    proposed: int = 0
    malformed: int = 0
    gate1_rejected: int = 0
    killed: int = 0
    survivors: int = 0
    proven: int = 0
    attempts_to_proof: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0
    blind_spots: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def pick_test_file(repo_dir: Path, preferred: str) -> str:
    if (repo_dir / preferred).is_file():
        return preferred
    candidates = sorted((repo_dir / "tests").glob("test_*.py"), key=lambda p: -p.stat().st_size)
    return str(candidates[0].relative_to(repo_dir)) if candidates else preferred


def main() -> int:
    client = NemotronClient(cap_usd=2.00)
    start_spend = client.ledger.total_usd
    results: list[TargetResult] = []

    for repo, module, function, import_name, test_file, selector in TARGETS:
        repo_dir = CHECKOUTS / repo
        python_exe = str(repo_dir / ".venv" / "bin" / "python")
        res = TargetResult(repo=repo, function=function)
        t0 = time.monotonic()
        print(f"\n{'='*74}\n{repo}  ::  {function}\n{'='*74}", flush=True)

        try:
            mutations, rejected = generate_mutations(
                client, repo_dir, module, function, n=N_MUTATIONS)
        except BudgetExceeded as exc:
            print(f"  budget: {exc}")
            break
        res.proposed = len(mutations) + len(rejected)
        res.malformed = len(rejected)
        print(f"  proposed {res.proposed}, {len(mutations)} applied cleanly"
              + (f", {len(rejected)} malformed" if rejected else ""), flush=True)
        for r in rejected[:2]:
            print(f"    dropped: {r[:100]}")

        survivors = []
        for m in mutations:
            plaus = gate1.check(m, repo_dir, python_exe)
            if not plaus.ok:
                res.gate1_rejected += 1
                print(f"  [gate1] {m.bug_class:24s} rejected: {plaus.reason[:60]}", flush=True)
                continue
            with m.applied(repo_dir):
                run = run_pytest(repo_dir, selector, python_exe, timeout=600)
            if run.verdict == PASSED:
                survivors.append(m)
                print(f"  [SURVIVED] {m.bug_class:22s} {m.original.strip()[:28]!r}"
                      f" -> {m.mutated.strip()[:28]!r}", flush=True)
            else:
                res.killed += 1
        res.survivors = len(survivors)
        print(f"  -> {res.killed} killed, {res.survivors} survived", flush=True)

        real_test_file = pick_test_file(repo_dir, test_file)
        for m in survivors:
            try:
                attempts = generate_proof_test(
                    client, repo_dir, m, import_name, function.split(".")[-1],
                    real_test_file, max_attempts=MAX_PROOF_ATTEMPTS,
                    python_exe=python_exe, repeats=1,
                )
            except BudgetExceeded as exc:
                res.notes.append(f"budget stopped proof generation: {exc}")
                print(f"  budget: {exc}")
                break
            res.cost_usd += sum(a.cost_usd for a in attempts)
            final = attempts[-1]
            if final.ok:
                res.proven += 1
                res.attempts_to_proof.append(final.attempt)
                res.blind_spots.append({
                    "mutation": f"{m.original.strip()} -> {m.mutated.strip()}",
                    "line": m.line, "bug_class": m.bug_class,
                    "attempts": final.attempt, "proof": final.source,
                })
                print(f"  [PROVEN in {final.attempt}] {m.bug_class}", flush=True)
            else:
                why = "; ".join(r.name.split()[0] for r in final.gate.failures)
                print(f"  [unproven after {len(attempts)}] {m.bug_class} — blocked on rule {why}",
                      flush=True)

        res.seconds = round(time.monotonic() - t0, 1)
        results.append(res)

    # ---------------------------------------------------------------- summary
    print(f"\n\n{'='*74}\nPHASE 1 RESULTS\n{'='*74}")
    print(f"{'repo':<15}{'function':<24}{'mut':>4}{'kill':>5}{'surv':>5}{'proven':>7}{'time':>7}")
    print("-" * 74)
    tot = TargetResult(repo="", function="TOTAL")
    for r in results:
        applied = r.proposed - r.malformed - r.gate1_rejected
        print(f"{r.repo:<15}{r.function:<24}{applied:>4}{r.killed:>5}"
              f"{r.survivors:>5}{r.proven:>7}{r.seconds:>6.0f}s")
        for f_ in ("proposed", "malformed", "gate1_rejected", "killed", "survivors", "proven"):
            setattr(tot, f_, getattr(tot, f_) + getattr(r, f_))
        tot.attempts_to_proof += r.attempts_to_proof
        tot.seconds += r.seconds
    print("-" * 74)
    applied = tot.proposed - tot.malformed - tot.gate1_rejected
    print(f"{'':<15}{'TOTAL':<24}{applied:>4}{tot.killed:>5}{tot.survivors:>5}"
          f"{tot.proven:>7}{tot.seconds:>6.0f}s")

    print()
    if tot.survivors:
        rate = 100 * tot.proven / tot.survivors
        mean = (sum(tot.attempts_to_proof) / len(tot.attempts_to_proof)
                if tot.attempts_to_proof else 0)
        print(f"  PROOF-TEST GATE PASS RATE: {tot.proven}/{tot.survivors} = {rate:.0f}%")
        print(f"  mean attempts to a proof:  {mean:.1f}")
    else:
        print("  no survivors — nothing to prove")
    print(f"  attacks discarded by gate 1: {tot.gate1_rejected}"
          f"  |  malformed from the model: {tot.malformed}")
    print(f"  spend this run: ${client.ledger.total_usd - start_spend:.4f}")
    print()
    print(client.ledger.summary())

    out = ROOT / "results" / f"phase1-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\n  written: {out.relative_to(Path.home())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
