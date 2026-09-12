#!/usr/bin/env python
"""Phase 1b — the same question, asked of code that was actually under review.

Phase 1 pointed MUTINY at mature library internals and 27 of 30 attacks died.
That is the right answer for code that has been settling for years, and it is
also the wrong place to measure: there is nothing to find, so there is no
denominator.

This run attacks real commits instead — bug fixes and newly added functions,
mutating only the lines the change introduced. That is what the product does at
pull-request time, and it is where test gaps actually live.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import gate1
from mutiny.attacks import function_span, generate_mutations
from mutiny.coverage import covering_examples, measure
from mutiny.diff import changed_lines, enclosing_functions
from mutiny.models import BudgetExceeded, NemotronClient
from mutiny.proof import generate_proof_test
from mutiny.runner import PASSED, SELECTION_ERROR, run_pytest

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT / "checkouts"

# (repo, commit, import name, suite selector) - real fixes and new functions
COMMITS = [
    ("python-semver", "fdec4ae1", "semver", "tests/"),
    ("python-semver", "d8813b67", "semver", "tests/"),
    ("cachetools", "dd181c5a", "cachetools", "tests/"),
    ("cachetools", "39b31bc9", "cachetools", "tests/"),
    ("packaging", "053c8846", "packaging", "tests/test_tags.py"),
    ("packaging", "55cbf1b9", "packaging", "tests/"),
]

N_MUTATIONS = 6
MAX_ATTEMPTS = 3


@contextmanager
def at_commit(repo: Path, sha: str):
    """Check out `sha`, restore whatever was there afterwards."""
    original = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
        capture_output=True, text=True).stdout.strip()
    if original == "HEAD":
        original = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo,
            capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "stash", "-u", "-q"], cwd=repo, capture_output=True)
    subprocess.run(["git", "checkout", "-q", sha], cwd=repo, check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-q", original], cwd=repo, capture_output=True)


@dataclass
class Result:
    repo: str
    sha: str
    subject: str = ""
    changed_lines: int = 0
    applied: int = 0
    malformed: int = 0
    gate1_rejected: int = 0
    killed: int = 0
    survivors: int = 0
    proven: int = 0
    attempts: list[int] = field(default_factory=list)
    seconds: float = 0.0
    coverage_seconds: float = 0.0
    uncovered_rejected: int = 0
    selection_fallbacks: int = 0
    blind_spots: list[dict] = field(default_factory=list)
    unproven: list[dict] = field(default_factory=list)


def main() -> int:
    client = NemotronClient(cap_usd=3.00)
    opening = client.ledger.total_usd
    results: list[Result] = []

    for repo_name, sha, import_name, selector in COMMITS:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        res = Result(repo=repo_name, sha=sha[:8])
        t0 = time.monotonic()

        with at_commit(repo, sha):
            res.subject = subprocess.run(
                ["git", "log", "-1", "--format=%s"], cwd=repo,
                capture_output=True, text=True).stdout.strip()[:60]
            print(f"\n{'='*76}\n{repo_name} @ {sha[:8]}  {res.subject}\n{'='*76}", flush=True)

            files = changed_lines(repo, sha)
            if not files:
                print("  no source lines changed"); continue
            target = max(files, key=len)
            res.changed_lines = len(target)
            source = (repo / target.path).read_text(encoding="utf-8")
            fns = enclosing_functions(source, target.lines)
            if not fns:
                print(f"  {target.path}: changed lines are not inside a function"); continue
            function = fns[0]
            lo, hi = function_span(source, function)
            in_fn = tuple(n for n in target.lines if lo <= n <= hi) or target.lines
            print(f"  {target.path}  {function}  ({len(in_fn)} changed lines)", flush=True)

            # One instrumented run of the suite, before any mutation. It answers two
            # questions at once: whether a line is covered at all (Gate 1), and
            # which tests reach it (the proof-test prompt, and the selector below).
            cov_t0 = time.monotonic()
            cov = measure(repo, selector, import_name.split(".")[0], python_exe)
            res.coverage_seconds = round(time.monotonic() - cov_t0, 1)
            print(f"  coverage: {len(cov.by_line)} lines mapped in "
                  f"{res.coverage_seconds}s" + ("" if cov.measured else f" — {cov.note}"),
                  flush=True)

            try:
                muts, rejected = generate_mutations(
                    client, repo, target.path, function, n=N_MUTATIONS, only_lines=in_fn)
            except (BudgetExceeded, ValueError) as exc:
                print(f"  skipped: {exc}"); continue
            res.applied, res.malformed = len(muts), len(rejected)
            print(f"  {len(muts)} mutations on changed lines"
                  + (f", {len(rejected)} rejected" if rejected else ""), flush=True)

            survivors = []
            for m in muts:
                p = gate1.check(m, repo, python_exe, coverage=cov)
                if not p.ok:
                    res.gate1_rejected += 1
                    if "uncovered code" in p.reason:
                        res.uncovered_rejected += 1
                    print(f"  [gate1] {m.bug_class:22s} {p.reason[:52]}", flush=True)
                    continue
                # A test that never executes the mutated line cannot be affected by
                # it, so running the whole suite per mutant is wasted work. This is
                # where the 800-second packaging runs went.
                covering = cov.tests_for(m.path, m.line, limit=200)
                mutant_selector = list(covering) if covering else [selector]
                with m.applied(repo):
                    run = run_pytest(repo, mutant_selector, python_exe, timeout=900)
                    if run.verdict == SELECTION_ERROR:
                        # A node id coverage gave us no longer resolves — usually a
                        # parametrise id that does not round-trip. Fall back rather
                        # than score the mutant on a run that never happened.
                        res.selection_fallbacks += 1
                        run = run_pytest(repo, selector, python_exe, timeout=900)
                if run.verdict == PASSED:
                    survivors.append(m)
                    print(f"  [SURVIVED] L{m.line} {m.bug_class:20s} "
                          f"{m.original.strip()[:26]!r} -> {m.mutated.strip()[:26]!r}", flush=True)
                else:
                    res.killed += 1
            res.survivors = len(survivors)
            print(f"  -> {res.killed} killed, {res.survivors} survived", flush=True)

            for m in survivors:
                examples = covering_examples(repo, cov, m.path, m.line, limit=3)
                print(f"     showing {len(examples)} covering test(s) as worked examples",
                      flush=True)
                try:
                    attempts = generate_proof_test(
                        client, repo, m, import_name, function.split(".")[-1],
                        covering_tests=examples,
                        max_attempts=MAX_ATTEMPTS, python_exe=python_exe, repeats=1)
                except BudgetExceeded as exc:
                    print(f"  budget: {exc}"); break
                final = attempts[-1]
                if final.ok:
                    res.proven += 1
                    res.attempts.append(final.attempt)
                    res.blind_spots.append({
                        "line": m.line, "bug_class": m.bug_class,
                        "mutation": f"{m.original.strip()} -> {m.mutated.strip()}",
                        "attempts": final.attempt, "proof": final.source})
                    print(f"  [PROVEN in {final.attempt}] L{m.line} {m.bug_class}", flush=True)
                else:
                    rules = ",".join(r.name.split()[0] for r in final.gate.failures)
                    res.unproven.append({
                        "line": m.line, "bug_class": m.bug_class,
                        "mutation": f"{m.original.strip()} -> {m.mutated.strip()}",
                        "blocked_on": rules, "attempts": len(attempts),
                        "covering_tests": list(cov.tests_for(m.path, m.line)),
                        "last_test": final.source,
                        "gate": [f"{r.name}: {r.detail}" for r in final.gate.failures],
                    })
                    print(f"  [unproven/{len(attempts)}] L{m.line} {m.bug_class} "
                          f"— blocked on rule {rules}", flush=True)

        res.seconds = round(time.monotonic() - t0, 1)
        results.append(res)

    print(f"\n\n{'='*76}\nPHASE 1b — DIFF-SCOPED\n{'='*76}")
    print(f"{'repo':<15}{'commit':<10}{'mut':>4}{'kill':>5}{'surv':>5}{'proven':>7}{'time':>7}")
    print("-" * 76)
    tm = tk = ts = tp = 0
    all_attempts: list[int] = []
    for r in results:
        applied = r.applied - r.gate1_rejected
        print(f"{r.repo:<15}{r.sha:<10}{applied:>4}{r.killed:>5}{r.survivors:>5}"
              f"{r.proven:>7}{r.seconds:>6.0f}s")
        tm += applied; tk += r.killed; ts += r.survivors; tp += r.proven
        all_attempts += r.attempts
    print("-" * 76)
    print(f"{'':<15}{'TOTAL':<10}{tm:>4}{tk:>5}{ts:>5}{tp:>7}")
    print()
    if ts:
        print(f"  PROOF-TEST GATE PASS RATE: {tp}/{ts} = {100*tp/ts:.0f}%")
        if all_attempts:
            print(f"  mean attempts to a proof:  {sum(all_attempts)/len(all_attempts):.1f}")
    if tm:
        print(f"  survival rate on changed lines: {ts}/{tm} = {100*ts/tm:.0f}%"
              f"   (Phase 1 on mature internals: 10%)")
    print(f"  spend this run: ${client.ledger.total_usd - opening:.4f}")
    print()
    print(client.ledger.summary())

    out = ROOT / "results" / f"phase1b-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
