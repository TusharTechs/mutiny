#!/usr/bin/env python
"""Does differential execution detect a real behaviour change, and stay quiet
when nothing changed?

Ground truth is free: a commit whose message says "fix" changed behaviour and
must produce a divergence, and one that says "stylistic improvements" did not
and must produce none. A harness that fires on both is a noise generator; one
that fires on neither is blind.

Inputs are generated from the *before* version only, so nothing about the fix
leaks into the inputs that are supposed to catch it.
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny.attacks import focused_module
from mutiny.differential import agreement, compare, observe
from mutiny.coverage import covering_examples, measure
from mutiny.inputs import generate_validated, receiver_candidates
from mutiny.models import NemotronClient

CHECKOUTS = Path(__file__).resolve().parents[1] / "phase1" / "checkouts"

# (repo, commit, import module, qualname, expect divergence, why)
CASES = [
    ("python-semver", "fdec4ae1", "semver", "Version.next_version", True,
     "fix(next_version): reset prerelease when token changes"),
    ("python-semver", "d8813b67", "semver", "Version.bump_prerelease", True,
     "Fix #460: bump_prerelease should always get a newer version"),
    ("cachetools", "39b31bc9", "cachetools", "Cache.__setitem__", True,
     "Fix #405: __setitem__ over-evicting when growing an item"),
    ("cachetools", "ccaa8c8c", "cachetools", "TLRUCache.__setitem__", False,
     "Minor stylistic improvements"),
    ("cachetools", "13bb86a5", "cachetools", "Cache.__init__", False,
     "Minor style improvements to keep ruff happy"),
    ("packaging", "55cbf1b9", "packaging._ranges", "UpperBound.__init__", True,
     "fix(ranges): stop unbounded bounds sorting above each other"),
]
N_INPUTS = 45


@contextmanager
def at(repo: Path, ref: str):
    original = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=repo, check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-q",
                        branch if branch != "HEAD" else original],
                       cwd=repo, capture_output=True)


def guess_suite(repo: Path, source_path: str) -> str:
    stem = Path(source_path).stem.lstrip("_")
    for c in (f"tests/test_{stem}.py", f"tests/{stem}_test.py"):
        if (repo / c).is_file():
            return c
    return "tests/"


def source_at(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


def find_path(repo: Path, ref: str, qualname: str) -> str | None:
    """The changed source file that actually defines this function."""
    from mutiny.diff import changed_lines
    for cf in changed_lines(repo, ref):
        try:
            focused_module(source_at(repo, ref, cf.path), qualname)
            return cf.path
        except ValueError:
            continue
    return None


def main() -> int:
    client = NemotronClient(cap_usd=8.00)
    opening = client.ledger.total_usd
    rows = []

    for repo_name, sha, module, qualname, expect, why in CASES:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        print(f"\n{'='*78}\n{repo_name} @ {sha}  {why}\n  {qualname}  expect "
              f"{'DIVERGENCE' if expect else 'no change'}\n{'='*78}", flush=True)

        path = find_path(repo, sha, qualname)
        if path is None:
            print("  SKIP — cannot locate the function in the changed files")
            continue

        before_src = focused_module(source_at(repo, f"{sha}^", path), qualname)

        try:
            with at(repo, f"{sha}^"):
                # Existing tests show how these objects are really constructed;
                # guessing a constructor's signature is how 45 inputs ended up
                # failing identically on both sides, which reads as agreement.
                examples = []
                cov = measure(repo, guess_suite(repo, path), module.split(".")[0], python_exe)
                if cov.measured:
                    lines = sorted(cov.covered_lines(path))
                    for ln in lines:
                        examples = covering_examples(repo, cov, path, ln, limit=2)
                        if examples:
                            break

                subclasses = receiver_candidates(
                    source_at(repo, f"{sha}^", path), qualname)
                exprs, usable_obs = generate_validated(
                    client, module, qualname, before_src,
                    probe=lambda e: observe(repo, module, e, python_exe),
                    n=N_INPUTS, covering_tests=examples, subclasses=subclasses)
                print(f"  {len(exprs)} inputs, {len(usable_obs)} run cleanly on the "
                      f"pre-change code" + (f" ({len(examples)} test examples shown)"
                                            if examples else ""), flush=True)
                if not exprs:
                    print("  SKIP — no usable inputs")
                    continue
                before = observe(repo, module, exprs, python_exe)
            with at(repo, sha):
                after = observe(repo, module, exprs, python_exe)
        except Exception as exc:
            print(f"  ERROR: {type(exc).__name__}: {str(exc)[:160]}")
            continue

        compared, usable, diverged = agreement(before, after)
        divs = compare(before, after)
        correct = bool(diverged) == expect
        print(f"  {compared} compared, {usable} accepted by both, "
              f"{diverged} divergent   -> {'CORRECT' if correct else 'WRONG'}", flush=True)
        for d in divs[:3]:
            print("    " + str(d).replace("\n", "\n  "), flush=True)
        rows.append((repo_name, sha, qualname, expect, diverged, usable, compared, correct))

    print(f"\n\n{'='*78}\nDIFFERENTIAL VALIDATION\n{'='*78}")
    print(f"{'repo':<14}{'commit':<10}{'expect':<9}{'found':>6}{'usable':>8}{'verdict':>10}")
    print("-" * 78)
    for repo_name, sha, _q, expect, diverged, usable, compared, correct in rows:
        print(f"{repo_name:<14}{sha:<10}{'diverge' if expect else 'same':<9}"
              f"{diverged:>6}{usable:>4}/{compared:<3}{'CORRECT' if correct else 'WRONG':>10}")
    print("-" * 78)
    if rows:
        right = sum(r[-1] for r in rows)
        print(f"  {right}/{len(rows)} correct")
        tp = sum(1 for r in rows if r[3] and r[4])
        fn = sum(1 for r in rows if r[3] and not r[4])
        fp = sum(1 for r in rows if not r[3] and r[4])
        tn = sum(1 for r in rows if not r[3] and not r[4])
        print(f"  behaviour changes detected: {tp}/{tp+fn}")
        print(f"  refactors correctly silent: {tn}/{tn+fp}")
        usable_total = sum(r[5] for r in rows)
        compared_total = sum(r[6] for r in rows)
        if compared_total:
            print(f"  inputs accepted by both versions: {usable_total}/{compared_total} "
                  f"= {100*usable_total/compared_total:.0f}%")
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
