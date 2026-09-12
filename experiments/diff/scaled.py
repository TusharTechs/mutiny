#!/usr/bin/env python
"""Measure differential detection across many commits, with repeats.

Five cases and a temperature-0.7 generator produced run-to-run swings larger
than any change being evaluated. This discovers commits automatically, takes
ground truth from their messages, and runs each case more than once so variance
is visible instead of confounding.

One honest caveat, encoded in the reporting: a human "refactor" commit is
*supposed* to preserve behaviour but sometimes does not — published work puts
19-35% of LLM refactorings in that category and humans are not immune. So a
divergence on a refactor is not automatically a false positive. Those are
reported separately for inspection rather than scored as errors.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny.attacks import focused_module
from mutiny.coverage import covering_examples, measure
from mutiny.differential import compare, observe
from mutiny.diff import changed_lines, enclosing_functions, source_commits
from mutiny.inputs import generate_validated, receiver_candidates
from mutiny.models import BudgetExceeded, NemotronClient

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT.parent / "phase1" / "checkouts"

REPOS = {"python-semver": "semver", "cachetools": "cachetools", "packaging": "packaging"}
N_INPUTS = 45
REPEATS = 2
MIN_LINES, MAX_LINES = 2, 60
CAP_USD = 10.00

# Deliberately conservative. A message that does not clearly say which kind of
# change it is gets skipped rather than guessed at.
CHANGES_BEHAVIOUR = re.compile(
    r"^(fix|bugfix)\b|\bfix(es|ed)?\s+#\d+|\bbug\b|\bregression\b|\bincorrect\b", re.I)
PRESERVES_BEHAVIOUR = re.compile(
    r"\b(style|styling|stylistic|typo|formatting|lint|ruff|black|flake8|isort|"
    r"whitespace|docstring|comment|rename|cleanup|clean up|cosmetic|readability|"
    r"simplif|refactor)\b", re.I)


@contextmanager
def at(repo: Path, ref: str):
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "checkout", "-q", ref], cwd=repo, check=True, capture_output=True)
    try:
        yield
    finally:
        subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "checkout", "-q", branch if branch != "HEAD" else head],
                       cwd=repo, capture_output=True)


def source_at(repo: Path, ref: str, path: str) -> str:
    return subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout


def module_for(path: str) -> str:
    """Import name for a source file: src/packaging/_ranges.py -> packaging._ranges.

    Using the top-level package instead is silent and total: every expression
    raises NameError because the symbols live in a submodule, and the run reports
    zero usable inputs rather than an error. It cost all eight packaging cases.
    """
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def suite_for(repo: Path, path: str) -> str:
    stem = Path(path).stem.lstrip("_")
    for c in (f"tests/test_{stem}.py", f"tests/{stem}_test.py"):
        if (repo / c).is_file():
            return c
    return "tests/"


def classify(subject: str) -> bool | None:
    fixes, preserves = CHANGES_BEHAVIOUR.search(subject), PRESERVES_BEHAVIOUR.search(subject)
    if fixes and not preserves:
        return True
    if preserves and not fixes:
        return False
    return None


def discover(repo: Path, limit: int) -> list[dict]:
    out = []
    for sha in source_commits(repo, limit=limit * 10):
        subject = subprocess.run(["git", "log", "-1", "--format=%s", sha], cwd=repo,
                                 capture_output=True, text=True).stdout.strip()
        expect = classify(subject)
        if expect is None:
            continue
        files = changed_lines(repo, sha)
        if not files:
            continue
        target = max(files, key=len)
        if not (MIN_LINES <= len(target) <= MAX_LINES):
            continue
        try:
            src = source_at(repo, sha, target.path)
        except subprocess.CalledProcessError:
            continue
        fns = enclosing_functions(src, target.lines)
        if not fns:
            continue
        fn = fns[0]
        try:
            focused_module(source_at(repo, f"{sha}^", target.path), fn)
        except (ValueError, subprocess.CalledProcessError):
            continue
        out.append({"sha": sha, "subject": subject[:62], "path": target.path,
                    "fn": fn, "expect": expect})
        if len(out) >= limit:
            break
    return out


def one_run(client, repo, module, case, python_exe) -> dict:
    sha, path, fn = case["sha"], case["path"], case["fn"]
    module = module_for(path) or module
    before_src = focused_module(source_at(repo, f"{sha}^", path), fn)
    subclasses = receiver_candidates(source_at(repo, f"{sha}^", path), fn)

    with at(repo, f"{sha}^"):
        examples = []
        cov = measure(repo, suite_for(repo, path), module.split(".")[0], python_exe)
        if cov.measured:
            for ln in sorted(cov.covered_lines(path)):
                examples = covering_examples(repo, cov, path, ln, limit=2)
                if examples:
                    break
        exprs, _ = generate_validated(
            client, module, fn, before_src,
            probe=lambda e: observe(repo, module, e, python_exe),
            n=N_INPUTS, covering_tests=examples, subclasses=subclasses)
        if not exprs:
            return {"inputs": 0, "usable": 0, "diverged": 0, "witnesses": []}
        before = observe(repo, module, exprs, python_exe)
    with at(repo, sha):
        after = observe(repo, module, exprs, python_exe)

    divs = compare(before, after)
    idx = {o.input: o for o in after}
    usable = sum(1 for b in before if b.ok and idx.get(b.input) and idx[b.input].ok)
    return {"inputs": len(exprs), "usable": usable, "diverged": len(divs),
            "witnesses": [str(d) for d in divs[:2]]}


def main() -> int:
    per_repo = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8
    client = NemotronClient(cap_usd=CAP_USD)
    opening = client.ledger.total_usd
    out = ROOT / "results" / f"diff-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    plan = []
    for repo_name, module in REPOS.items():
        found = discover(CHECKOUTS / repo_name, per_repo)
        n_fix = sum(c["expect"] for c in found)
        print(f"{repo_name}: {len(found)} commits "
              f"({n_fix} behaviour-changing, {len(found)-n_fix} preserving)", flush=True)
        plan += [(repo_name, module, c) for c in found]
    print(f"\n{len(plan)} cases x {REPEATS} repeats\n", flush=True)

    records = []
    for repo_name, module, case in plan:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        kind = "CHANGES" if case["expect"] else "preserves"
        print(f"\n{repo_name} @ {case['sha'][:8]} [{kind}] {case['fn']}\n"
              f"  {case['subject']}", flush=True)
        runs = []
        for r in range(REPEATS):
            try:
                res = one_run(client, repo, module, case, python_exe)
            except BudgetExceeded as exc:
                print(f"  BUDGET: {exc}"); records.append({**case, "repo": repo_name,
                      "runs": runs}); out.write_text(json.dumps(records, indent=2)); return 0
            except Exception:
                res = {"inputs": 0, "usable": 0, "diverged": 0, "witnesses": [],
                       "error": traceback.format_exc().strip().splitlines()[-1][:140]}
            runs.append(res)
            print(f"  run {r+1}: {res['usable']}/{res['inputs']} usable, "
                  f"{res['diverged']} divergent"
                  + (f"  [{res['error']}]" if res.get("error") else ""), flush=True)
            for w in res["witnesses"][:1]:
                print("    " + w.replace("\n", "\n  "), flush=True)
        records.append({**case, "repo": repo_name, "runs": runs})
        out.write_text(json.dumps(records, indent=2))

    # ------------------------------------------------------------- summary
    print(f"\n\n{'='*78}\nDIFFERENTIAL DETECTION\n{'='*78}")
    print(f"{'repo':<14}{'commit':<10}{'kind':<11}{'divergent/run':<16}{'usable':>9}")
    print("-" * 78)
    agg = defaultdict(list)
    for rec in records:
        d = [r["diverged"] for r in rec["runs"]]
        u = [f"{r['usable']}/{r['inputs']}" for r in rec["runs"]]
        kind = "CHANGES" if rec["expect"] else "preserves"
        print(f"{rec['repo']:<14}{rec['sha'][:8]:<10}{kind:<11}"
              f"{str(d):<16}{', '.join(u):>9}")
        agg["detected" if rec["expect"] else "quiet"].append(any(x > 0 for x in d))
        agg["stability"].append(len(set(d)) == 1)
    print("-" * 78)
    fixes, refactors = agg["detected"], agg["quiet"]
    if fixes:
        print(f"  behaviour changes detected:   {sum(fixes)}/{len(fixes)} "
              f"= {100*sum(fixes)/len(fixes):.0f}%")
    if refactors:
        flagged = sum(refactors)
        print(f"  preserving commits flagged:   {flagged}/{len(refactors)}"
              f"   (inspect — a flagged refactor may be a real bug, not a false alarm)")
    if agg["stability"]:
        print(f"  runs agreeing across repeats: {sum(agg['stability'])}/"
              f"{len(agg['stability'])} = {100*sum(agg['stability'])/len(agg['stability']):.0f}%")
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
