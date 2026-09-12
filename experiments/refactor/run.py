#!/usr/bin/env python
"""Does the suite catch an agent's refactor when it breaks something — and do we?

Nemotron refactors a real function. The repository's own tests then judge it.
Three outcomes matter, and only one of them is interesting:

  rejected by the tests   the suite did its job; we add nothing
  passes, behaves same    a good refactor; we must stay silent
  passes, behaves differently   a break the suite missed — the whole point

The third number is the product. Published work puts 19-35% of LLM refactorings
as functionally incorrect, and reports that over 21% of those slip past existing
tests; this measures both on real code, and measures whether we catch them.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import refactor as R
from mutiny.attacks import focused_module, function_span
from mutiny.coverage import covering_examples, measure
from mutiny.differential import compare, observe
from mutiny.inputs import generate_validated, receiver_candidates
from mutiny.models import BudgetExceeded, NemotronClient
from mutiny.runner import PASSED, SELECTION_ERROR, run_pytest

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT.parent / "phase1" / "checkouts"

# (repo, source path, module, qualnames)
TARGETS = [
    ("python-semver", "src/semver/version.py", "semver.version",
     ["Version.next_version", "Version.bump_prerelease", "Version.compare",
      "Version.match", "Version.parse", "Version.bump_build", "Version.replace"]),
    ("cachetools", "src/cachetools/__init__.py", "cachetools",
     ["Cache.__setitem__", "LRUCache.popitem", "LFUCache.__setitem__",
      "TTLCache.expire", "FIFOCache.__setitem__", "Cache.__init__"]),
    ("packaging", "src/packaging/version.py", "packaging.version",
     ["_cmpkey", "_parse_letter_version", "Version.__init__", "parse"]),
    ("packaging", "src/packaging/tags.py", "packaging.tags",
     ["interpreter_abi", "parse_tag", "_generic_abi", "compatible_tags"]),
]
N_INPUTS = 45
CAP_USD = 14.00


@dataclass
class Case:
    repo: str
    qualname: str
    refactored: bool = False
    tests_pass: bool = False
    divergent: int = 0
    usable: int = 0
    inputs: int = 0
    witnesses: list = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def outcome(self) -> str:
        if not self.refactored:
            return "no refactor produced"
        if not self.inputs:
            return "unmeasured"
        if not self.tests_pass:
            return "broken; we CAUGHT it" if self.divergent else "broken; we MISSED it"
        return "BREAKS, tests missed it" if self.divergent else "preserved"


def restore(repo: Path, path: str) -> None:
    subprocess.run(["git", "checkout", "-q", "--", path], cwd=repo, capture_output=True)


def run_case(client, repo, repo_name, path, module, qualname, python_exe, suite) -> Case:
    c = Case(repo=repo_name, qualname=qualname)
    t0 = time.monotonic()
    original = (repo / path).read_text(encoding="utf-8")
    try:
        rf = R.refactor(client, original, qualname)
        if rf is None or not rf.changed:
            c.error = "model returned no usable rewrite"
            return c
        patched = R.apply(original, qualname, rf.rewritten)
        if patched is None:
            c.error = "rewrite did not apply cleanly"
            return c
        c.refactored = True

        cov = measure(repo, suite, module.split(".")[0], python_exe)
        covering = []
        if cov.measured:
            lo, hi = function_span(original, qualname)
            for ln in range(lo, hi + 1):
                covering += [t for t in cov.tests_for(path, ln, limit=40) if t not in covering]

        # The suite is the oracle. A refactor it rejects is already handled.
        (repo / path).write_text(patched, encoding="utf-8")
        try:
            run = run_pytest(repo, covering or [suite], python_exe, timeout=900)
            if run.verdict == SELECTION_ERROR:
                run = run_pytest(repo, suite, python_exe, timeout=900)
            c.tests_pass = run.verdict == PASSED
        finally:
            restore(repo, path)
        # Measure these too rather than returning. A refactor the tests rejected
        # is known-broken, which makes it ground truth: if the differential does
        # not flag it, it would not have been a safety net where the suite is
        # thinner — and most code has a thinner suite than these three.
        

        # Tests passed. Did behaviour actually survive?
        examples = covering_examples(repo, cov, path, function_span(original, qualname)[0], limit=2) \
            if cov.measured else []
        exprs, _ = generate_validated(
            client, module, qualname, focused_module(original, qualname),
            probe=lambda e: observe(repo, module, e, python_exe),
            n=N_INPUTS, covering_tests=examples,
            subclasses=receiver_candidates(original, qualname),
            diff=f"--- before\n+++ after\n{rf.original}\n=== rewritten ===\n{rf.rewritten}")
        c.inputs = len(exprs)
        if not exprs:
            return c

        before = observe(repo, module, exprs, python_exe)
        (repo / path).write_text(patched, encoding="utf-8")
        try:
            after = observe(repo, module, exprs, python_exe)
        finally:
            restore(repo, path)

        divs = compare(before, after)
        idx = {o.input: o for o in after}
        c.usable = sum(1 for b in before if b.ok and idx.get(b.input) and idx[b.input].ok)
        c.divergent = len(divs)
        c.witnesses = [str(d) for d in divs[:2]]
    except BudgetExceeded:
        raise
    except Exception:
        c.error = traceback.format_exc().strip().splitlines()[-1][:150]
    finally:
        restore(repo, path)
        c.seconds = round(time.monotonic() - t0, 1)
    return c


def main() -> int:
    client = NemotronClient(cap_usd=CAP_USD)
    opening = client.ledger.total_usd
    out = ROOT / "results" / f"refactor-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    cases: list[Case] = []

    for repo_name, path, module, qualnames in TARGETS:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        stem = Path(path).stem.lstrip("_")
        suite = f"tests/test_{stem}.py" if (repo / f"tests/test_{stem}.py").is_file() else "tests/"
        for qualname in qualnames:
            print(f"\n{repo_name}  {qualname}", flush=True)
            try:
                c = run_case(client, repo, repo_name, path, module, qualname, python_exe, suite)
            except BudgetExceeded as exc:
                print(f"  BUDGET: {exc}"); break
            cases.append(c)
            print(f"  {c.outcome}"
                  + (f"   ({c.divergent} divergent of {c.usable} usable)" if c.inputs else "")
                  + (f"   [{c.error}]" if c.error else ""), flush=True)
            for w in c.witnesses[:1]:
                print("    " + w.replace("\n", "\n  ")[:340], flush=True)
            out.write_text(json.dumps([asdict(x) for x in cases], indent=2))

    print(f"\n\n{'='*78}\nAGENT REFACTOR SAFETY\n{'='*78}")
    print(f"{'repo':<15}{'function':<30}{'outcome':<28}{'div':>4}")
    print("-" * 78)
    for c in cases:
        print(f"{c.repo:<15}{c.qualname:<30}{c.outcome:<28}{c.divergent:>4}")
    print("-" * 78)
    produced = [c for c in cases if c.refactored]
    rejected = [c for c in produced if not c.tests_pass and c.inputs]
    passed = [c for c in produced if c.tests_pass and c.inputs]
    caught = [c for c in rejected if c.divergent]
    missed_by_tests = [c for c in passed if c.divergent]
    print(f"  refactors produced:               {len(produced)}/{len(cases)}")
    if produced:
        n_bad = len([c for c in produced if not c.tests_pass])
        print(f"  broken (tests rejected them):     {n_bad}/{len(produced)}"
              f" = {100*n_bad/len(produced):.0f}%"
              f"   (published: 19-35% of LLM refactors are incorrect)")
    if rejected:
        print(f"  of the known-broken refactors:")
        print(f"    differential also caught:       {len(caught)}/{len(rejected)}"
              f" = {100*len(caught)/len(rejected):.0f}%"
              f"   <- would we be a safety net where tests are thinner?")
    if passed:
        print(f"  of the refactors tests ACCEPTED:")
        print(f"    behaviour changed anyway:       {len(missed_by_tests)}/{len(passed)}")
        print(f"    behaviour preserved (silent):   {len(passed)-len(missed_by_tests)}/{len(passed)}")
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
