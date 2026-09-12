#!/usr/bin/env python
"""Phase 2 — enough survivors for the number to mean something.

Phase 1 and its variants kept producing two or three survivors, which cannot
answer "does this work" however cleanly the run executes. This discovers commits
automatically across several repositories and keeps going until the denominator
is worth quoting.

Usage:  run.py [commits-per-repo] [--repos a,b]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import gate1
from mutiny.attacks import function_span, generate_mutations
from mutiny.coverage import covering_examples, measure
from mutiny.diff import changed_lines, enclosing_functions, source_commits
from mutiny.models import LIGHTNING, NANO, SUPER, ULTRA, BudgetExceeded, NemotronClient
from mutiny.proof import generate_proof_test
from mutiny.runner import PASSED, SELECTION_ERROR, run_pytest

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT.parent / "phase1" / "checkouts"

REPOS = {
    "python-semver": "semver",
    "cachetools": "cachetools",
    "packaging": "packaging",
}
PROOF_MODEL = {"nano": NANO, "lightning": LIGHTNING, "super": SUPER,
               "ultra": ULTRA}[os.environ.get("MUTINY_PROOF_MODEL", "super").lower()]
N_MUTATIONS = 6
MAX_ATTEMPTS = 3
MIN_LINES, MAX_LINES = 2, 60
CAP_USD = 6.00


@contextmanager
def at_commit(repo: Path, sha: str):
    original = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
    if original == "HEAD":
        original = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "checkout", "-q", sha], cwd=repo, check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-q", original], cwd=repo, capture_output=True)


def guess_selector(repo: Path, source_path: str) -> str:
    """Prefer the test file named after the module — running the whole suite for
    coverage costs two minutes on packaging and seconds on its own test file."""
    stem = Path(source_path).stem.lstrip("_")
    for candidate in (f"tests/test_{stem}.py", f"tests/{stem}_test.py",
                      f"test/test_{stem}.py"):
        if (repo / candidate).is_file():
            return candidate
    return "tests/"


def discover(repo: Path, limit: int) -> list[tuple[str, str, object, str]]:
    """Commits whose changed source lines sit inside one resolvable function."""
    found = []
    for sha in source_commits(repo, limit=limit * 8):
        files = changed_lines(repo, sha)
        if not files:
            continue
        target = max(files, key=len)
        if not (MIN_LINES <= len(target) <= MAX_LINES):
            continue
        try:
            src = subprocess.run(["git", "show", f"{sha}:{target.path}"], cwd=repo,
                                 capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        fns = enclosing_functions(src, target.lines)
        if not fns:
            continue
        fn = fns[0]
        try:
            function_span(src, fn)  # must resolve unambiguously
        except ValueError:
            continue
        subject = subprocess.run(["git", "log", "-1", "--format=%s", sha], cwd=repo,
                                 capture_output=True, text=True).stdout.strip()[:58]
        found.append((sha, subject, target, fn))
        if len(found) >= limit:
            break
    return found


@dataclass
class Target:
    repo: str
    sha: str
    subject: str = ""
    function: str = ""
    applied: int = 0
    malformed: int = 0
    gate1_rejected: int = 0
    uncovered: int = 0
    killed: int = 0
    survivors: int = 0
    proven: int = 0
    attempts: list[int] = field(default_factory=list)
    empty_replies: int = 0
    seconds: float = 0.0
    blind_spots: list[dict] = field(default_factory=list)
    unproven: list[dict] = field(default_factory=list)
    error: str = ""


def run_target(client, repo_name, repo, sha, subject, changed, fn, out) -> Target:
    t = Target(repo=repo_name, sha=sha[:8], subject=subject, function=fn)
    t0 = time.monotonic()
    python_exe = str(repo / ".venv" / "bin" / "python")
    package = REPOS[repo_name]

    with at_commit(repo, sha):
        selector = guess_selector(repo, changed.path)
        print(f"\n{'='*78}\n{repo_name} @ {sha[:8]}  {subject}\n{'='*78}", flush=True)
        print(f"  {changed.path}  {fn}  ({len(changed)} changed lines)  suite={selector}",
              flush=True)

        cov = measure(repo, selector, package, python_exe)
        if not cov.measured:
            t.error = f"coverage failed: {cov.note[:120]}"
            print(f"  SKIP — {t.error}", flush=True)
            t.seconds = round(time.monotonic() - t0, 1)
            return t
        print(f"  coverage: {len(cov.by_line)} lines mapped", flush=True)

        src = (repo / changed.path).read_text(encoding="utf-8")
        lo, hi = function_span(src, fn)
        in_fn = tuple(n for n in changed.lines if lo <= n <= hi) or changed.lines

        muts, rejected = generate_mutations(
            client, repo, changed.path, fn, n=N_MUTATIONS, only_lines=in_fn)
        t.applied, t.malformed = len(muts), len(rejected)
        print(f"  {len(muts)} mutations"
              + (f", {len(rejected)} rejected" if rejected else ""), flush=True)

        survivors = []
        for m in muts:
            p = gate1.check(m, repo, python_exe, coverage=cov)
            if not p.ok:
                t.gate1_rejected += 1
                t.uncovered += "uncovered code" in p.reason
                continue
            covering = cov.tests_for(m.path, m.line, limit=200)
            with m.applied(repo):
                run = run_pytest(repo, list(covering) or [selector], python_exe, timeout=900)
                if run.verdict == SELECTION_ERROR:
                    run = run_pytest(repo, selector, python_exe, timeout=900)
            if run.verdict == PASSED:
                survivors.append(m)
                print(f"  [SURVIVED] L{m.line} {m.bug_class:22s} "
                      f"{m.original.strip()[:24]!r} -> {m.mutated.strip()[:24]!r}", flush=True)
            else:
                t.killed += 1
        t.survivors = len(survivors)

        for m in survivors:
            examples = covering_examples(repo, cov, m.path, m.line, limit=3)
            attempts = generate_proof_test(
                client, repo, m, package, fn, covering_tests=examples,
                model=PROOF_MODEL, max_attempts=MAX_ATTEMPTS,
                python_exe=python_exe, repeats=1)
            final = attempts[-1]
            record = {"line": m.line, "bug_class": m.bug_class,
                      "mutation": f"{m.original.strip()} -> {m.mutated.strip()}",
                      "attempts": final.attempt,
                      "covering_tests": list(cov.tests_for(m.path, m.line)),
                      "test": final.source}
            if final.ok:
                t.proven += 1
                t.attempts.append(final.attempt)
                t.blind_spots.append(record)
                print(f"  [PROVEN in {final.attempt}] L{m.line} {m.bug_class}", flush=True)
            else:
                record["gate"] = [f"{r.name}: {r.detail}" for r in final.gate.failures]
                t.empty_replies += any(r.name.startswith("0 ") for r in final.gate.failures)
                t.unproven.append(record)
                rules = ",".join(r.name.split()[0] for r in final.gate.failures)
                print(f"  [unproven/{len(attempts)}] L{m.line} {m.bug_class} "
                      f"— rule {rules}", flush=True)

    t.seconds = round(time.monotonic() - t0, 1)
    return t


def summarise(results: list[Target], client, opening: float) -> None:
    print(f"\n\n{'='*78}\nPHASE 2\n{'='*78}")
    print(f"{'repo':<15}{'commit':<10}{'mut':>4}{'kill':>5}{'surv':>5}{'prov':>5}{'time':>7}")
    print("-" * 78)
    agg = {k: 0 for k in ("applied", "gate1_rejected", "uncovered", "killed",
                          "survivors", "proven", "malformed", "empty_replies")}
    attempts: list[int] = []
    for r in results:
        net = r.applied - r.gate1_rejected
        flag = f"  [{r.error[:30]}]" if r.error else ""
        print(f"{r.repo:<15}{r.sha:<10}{net:>4}{r.killed:>5}{r.survivors:>5}"
              f"{r.proven:>5}{r.seconds:>6.0f}s{flag}")
        for k in agg:
            agg[k] += getattr(r, k)
        attempts += r.attempts
    print("-" * 78)
    net = agg["applied"] - agg["gate1_rejected"]
    print(f"{'':<15}{'TOTAL':<10}{net:>4}{agg['killed']:>5}{agg['survivors']:>5}"
          f"{agg['proven']:>5}")
    print()
    s, p = agg["survivors"], agg["proven"]
    if s:
        print(f"  PROOF-TEST GATE PASS RATE: {p}/{s} = {100*p/s:.0f}%")
        if attempts:
            print(f"  mean attempts to a proof:  {sum(attempts)/len(attempts):.1f}")
    else:
        print("  no survivors")
    if net:
        print(f"  survival rate: {s}/{net} = {100*s/net:.0f}%")
    print(f"  gate 1 rejected {agg['gate1_rejected']} "
          f"({agg['uncovered']} as uncovered)   malformed {agg['malformed']}")
    if agg["empty_replies"]:
        print(f"  empty model replies: {agg['empty_replies']}")
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    print()
    print(client.ledger.summary())


def main() -> int:
    per_repo = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    client = NemotronClient(cap_usd=CAP_USD)
    opening = client.ledger.total_usd
    out = ROOT / "results" / f"phase2-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    results: list[Target] = []

    plan: list[tuple] = []
    for repo_name in REPOS:
        repo = CHECKOUTS / repo_name
        found = discover(repo, per_repo)
        print(f"{repo_name}: {len(found)} usable commits", flush=True)
        plan += [(repo_name, repo, *f) for f in found]
    print(f"\n{len(plan)} targets total   proof model: "
          f"{PROOF_MODEL.split('/')[-1]}\n", flush=True)

    for repo_name, repo, sha, subject, changed, fn in plan:
        try:
            results.append(run_target(client, repo_name, repo, sha, subject, changed, fn, out))
        except BudgetExceeded as exc:
            print(f"\nBUDGET REACHED: {exc}", flush=True)
            break
        except Exception:
            t = Target(repo=repo_name, sha=sha[:8], subject=subject, function=fn,
                       error=traceback.format_exc().strip().splitlines()[-1][:160])
            results.append(t)
            print(f"  ERROR: {t.error}", flush=True)
        out.write_text(json.dumps([asdict(r) for r in results], indent=2))

    summarise(results, client, opening)
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
