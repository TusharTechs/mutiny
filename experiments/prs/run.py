#!/usr/bin/env python
"""Does this find things a reviewer would want to know?

Every number measured so far answers a narrower question: can MUTINY detect a
behaviour change that was deliberately introduced, by a fix commit or by asking
a model to rewrite a function. That establishes the mechanism works. It does not
establish that anyone needs it.

This runs MUTINY over merged pull requests nobody chose for its benefit, and
splits the results by what each pull request *claimed about itself*:

  fix       the author says behaviour changes. Finding it is a sensitivity check.
  no-claim  refactor, cleanup, style, chore, perf, rename — the author is
            asserting that behaviour is preserved. A divergence here is either
            something the reviewer should see, or a false positive worth
            chasing. Both are worth knowing and neither is decided here.
  feature   new behaviour, expected to diverge.

The honest failure modes are recorded as first-class outcomes rather than
folded into "no divergence found": a repository that will not install, or a
function no generated input can construct, is not evidence of anything.

GitHub allows 60 unauthenticated API calls an hour, so every response is cached
on disk and the budget is tracked. Set GITHUB_TOKEN to lift the limit and widen
the corpus.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import remote
from mutiny.config import github_token
from mutiny.diff import _is_source
from mutiny.fetch import FetchError
from mutiny.session import verify_trees

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"

# Pure-Python libraries that install cleanly in a sandbox, chosen before any
# result was seen. Anything needing a C toolchain is excluded because the
# install failure would be recorded as a MUTINY outcome, which it is not.
REPOS = [
    "psf/requests",
    "Textualize/rich",
    "pallets/click",
    "pallets/jinja",
    "pallets/itsdangerous",
    "tkem/cachetools",
    "python-semver/python-semver",
    "skorokithakis/shortuuid",
    "dbader/schedule",
    "jd/tenacity",
    "more-itertools/more-itertools",
    "arrow-py/arrow",
]

CLAIMS_A_FIX = re.compile(
    r"^\s*(fix|bug|hotfix|bugfix)\b|^\s*fix[(:]|\bfixes #|\bregression\b", re.I)
CLAIMS_NO_CHANGE = re.compile(
    r"^\s*(refactor|chore|style|perf|cleanup|clean up|typo|lint|format|rename"
    r"|simplify|tidy|docs?|test|ci|build|deps|bump)\b|^\s*(refactor|chore|style|perf|docs|test|ci)[(:]"
    r"|\b(no functional change|non-functional|cosmetic|purely internal)\b", re.I)
CLAIMS_A_FEATURE = re.compile(r"^\s*(feat|add|implement|support|new)\b|^\s*feat[(:]", re.I)
ANYWHERE_NO_CHANGE = re.compile(
    r"\b(refactor\w*|cleanup|clean up|tidy|typo|lint|formatting|rename\w*|docs|"
    r"documentation|readme|changelog|comments?|whitespace|deprecat\w*)\b", re.I)


@dataclass
class Result:
    repo: str
    number: int
    title: str
    claim: str
    outcome: str = ""
    functions: int = 0
    probes: int = 0
    divergences: int = 0
    witnesses: list = field(default_factory=list)
    summary: str = ""
    install_mode: str = ""
    skipped: list = field(default_factory=list)
    seconds: float = 0.0
    cost: float = 0.0
    detail: str = ""


def classify(title: str, labels: list[str]) -> str:
    """What does this pull request say about its own behaviour?"""
    blob = f"{title} {' '.join(labels)}"
    if CLAIMS_A_FIX.search(title) or any("bug" in l.lower() for l in labels):
        return "fix"
    if CLAIMS_NO_CHANGE.search(title) or any(
            l.lower() in {"refactor", "chore", "cleanup", "style"} for l in labels):
        return "no-claim"
    if CLAIMS_A_FEATURE.search(blob):
        return "feature"
    # "Update docs" and "Tidy the parser" say the same thing as "docs:" and
    # "refactor:" without the prefix convention, so look anywhere as a fallback.
    if ANYWHERE_NO_CHANGE.search(title):
        return "no-claim"
    return "other"


class Budget:
    """GitHub's unauthenticated limit is 60/hour. Spend it deliberately."""

    def __init__(self) -> None:
        self.calls = 0
        self.authenticated = bool(github_token())
        self.ceiling = 4500 if self.authenticated else 55

    def take(self, n: int = 1) -> bool:
        if self.calls + n > self.ceiling:
            return False
        self.calls += n
        return True


def cached(key: str, produce):
    """Disk-cache an API response so re-running costs no budget."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (hashlib.sha256(key.encode()).hexdigest()[:20] + ".json")
    if path.is_file():
        return json.loads(path.read_text()), True
    value = produce()
    path.write_text(json.dumps(value))
    return value, False


def merged_pulls(repo: str, budget: Budget, want: int) -> list[dict]:
    """Recently merged pull requests, newest first."""
    owner, name = repo.split("/")

    def fetch():
        if not budget.take():
            raise RuntimeError("api budget exhausted")
        raw = remote._request(
            f"{remote.API}/repos/{owner}/{name}/pulls"
            f"?state=closed&per_page=50&sort=updated&direction=desc")
        return json.loads(raw)

    listing, hit = cached(f"pulls:{repo}", fetch)
    if not hit:
        time.sleep(1)
    merged = [p for p in listing if p.get("merged_at")]
    return merged[:want]


def change_for(repo: str, pr: dict, budget: Budget) -> remote.Change | None:
    """The merge base and the Python files this pull request touched.

    The listing already carries both SHAs, so only the comparison costs a call —
    half what remote.pull_request() would spend.
    """
    owner, name = repo.split("/")
    base, head = pr["base"]["sha"], pr["head"]["sha"]

    def fetch():
        if not budget.take():
            raise RuntimeError("api budget exhausted")
        return json.loads(remote._request(
            f"{remote.API}/repos/{owner}/{name}/compare/{base}...{head}"))

    comparison, hit = cached(f"compare:{repo}:{base}:{head}", fetch)
    if not hit:
        time.sleep(1)
    merge_base = comparison.get("merge_base_commit", {}).get("sha") or base
    paths = tuple(f["filename"] for f in comparison.get("files", [])
                  if f.get("filename", "").endswith(".py"))
    # A pull request that only touches its own tests has nothing for us to run.
    # Skipping it here saves warming a sandbox to discover that.
    if not any(_is_source(p) for p in paths):
        return None
    return remote.Change(owner, name, merge_base, head, paths, pr=pr["number"])


def verify(change: remote.Change, result: Result) -> Result:
    """Run MUTINY over one change and record what actually happened."""
    started = time.monotonic()
    try:
        checkout = remote.materialise(change)
    except FetchError as exc:
        result.outcome, result.detail = "fetch-failed", str(exc)[:160]
        return result

    try:
        saw_target = False
        for event in verify_trees(
            checkout.before, checkout.after,
            base=change.base, head=change.head, paths=change.paths,
            name=f"{change.owner}/{change.repo}", probes=20, forks=8,
            cap=0.30, max_functions=3,
        ):
            kind = event.get("type")
            if kind == "review":
                result.functions = len(event.get("targets") or [])
                result.skipped = [s.get("why", "") for s in (event.get("skipped") or [])]
            elif kind == "checkpoint":
                result.install_mode = event.get("mode", "")
            elif kind == "function":
                saw_target = True
            elif kind == "probes":
                result.probes += event.get("count", 0)
            elif kind == "no_probes":
                pass
            elif kind == "result":
                divs = event.get("divergences") or []
                result.divergences += len(divs)
                result.summary = result.summary or (event.get("summary") or "")
                for d in divs[:3]:
                    result.witnesses.append(
                        {"input": d["input"][:150],
                         "before": str(d["before"])[:90],
                         "after": str(d["after"])[:90]})
            elif kind == "verdict":
                result.cost = float(event.get("cost") or 0)
            elif kind == "error":
                result.detail = str(event.get("message"))[:160]
    except Exception as exc:  # noqa: BLE001 - a crash is a result too
        result.outcome, result.detail = "error", f"{type(exc).__name__}: {exc}"[:160]
    finally:
        checkout.cleanup()
        result.seconds = round(time.monotonic() - started, 1)

    if not result.outcome:
        if result.divergences:
            result.outcome = "divergence"
        elif result.probes and saw_target:
            result.outcome = "preserved"
        elif saw_target or result.functions:
            result.outcome = "no-probes"
        elif not result.skipped:
            # Nothing in the library changed at all: documentation, packaging,
            # or a pull request that only deletes code. There is no behaviour
            # here to preserve or break, and calling this a failure to find a
            # target blames the tool for the shape of the change.
            result.outcome = "nothing-to-verify"
        elif all("added in this change" in why for why in result.skipped):
            # Every candidate is new. New code has no earlier behaviour to
            # differ from; this is the one question differential verification
            # cannot be asked.
            result.outcome = "new-code"
        else:
            result.outcome = "no-target"
    return result


def main() -> int:
    per_repo = int(os.environ.get("PRS_PER_REPO", "4"))
    budget = Budget()
    print(f"github: {'authenticated' if budget.authenticated else 'anonymous'}, "
          f"budget {budget.ceiling} calls\n", flush=True)

    out = ROOT / "results" / f"prs-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []

    for repo in REPOS:
        try:
            pulls = merged_pulls(repo, budget, per_repo)
        except (RuntimeError, FetchError, Exception) as exc:  # noqa: BLE001
            print(f"{repo}: skipped ({type(exc).__name__}: {exc})"[:140], flush=True)
            continue
        print(f"\n{repo}  ({len(pulls)} merged)", flush=True)

        for pr in pulls:
            labels = [l.get("name", "") for l in pr.get("labels", [])]
            result = Result(repo=repo, number=pr["number"],
                            title=(pr.get("title") or "")[:110],
                            claim=classify(pr.get("title") or "", labels))
            try:
                change = change_for(repo, pr, budget)
            except (RuntimeError, FetchError, Exception) as exc:  # noqa: BLE001
                print(f"  #{pr['number']:<6} skipped ({exc})"[:130], flush=True)
                continue
            if change is None:
                continue

            print(f"  #{result.number:<6} [{result.claim:<8}] {result.title[:58]}",
                  flush=True)
            verify(change, result)
            results.append(result)
            mark = {"divergence": "DIVERGED", "preserved": "preserved"}.get(
                result.outcome, result.outcome)
            print(f"          {mark:<12} {result.probes:>3} probes  "
                  f"{result.seconds:>5.1f}s  ${result.cost:.4f}"
                  + (f"  {result.detail}" if result.detail else ""), flush=True)
            if result.witnesses:
                w = result.witnesses[0]
                print(f"          {w['input'][:70]}", flush=True)
                print(f"            {w['before'][:40]} -> {w['after'][:40]}", flush=True)
            out.write_text(json.dumps([asdict(r) for r in results], indent=2))

    report(results, budget)
    out.write_text(json.dumps([asdict(r) for r in results], indent=2))
    print(f"\n  written: {out.name}")
    return 0


def report(results: list[Result], budget: Budget) -> None:
    print(f"\n\n{'=' * 76}\nMERGED PULL REQUESTS\n{'=' * 76}")
    if not results:
        print("  nothing ran")
        return

    order = ["fix", "no-claim", "feature", "other"]
    outcomes = ["divergence", "preserved", "no-probes", "no-target",
                "new-code", "nothing-to-verify", "error", "fetch-failed"]
    print(f"{'claim':<10}{'n':>4}" + "".join(f"{o[:8]:>10}" for o in outcomes))
    print("-" * 92)
    for claim in order:
        rows = [r for r in results if r.claim == claim]
        if not rows:
            continue
        counts = [sum(1 for r in rows if r.outcome == o) for o in outcomes]
        print(f"{claim:<10}{len(rows):>4}" + "".join(f"{c:>10}" for c in counts))
    print("-" * 92)

    answerable = [r for r in results
                  if r.outcome not in ("nothing-to-verify", "new-code", "fetch-failed")]
    verified = [r for r in results if r.outcome in ("divergence", "preserved")]
    print(f"  had behaviour to check       {len(answerable)}/{len(results)}")
    print(f"  reached a verdict            {len(verified)}/{len(answerable)}")
    if verified:
        found = [r for r in verified if r.outcome == "divergence"]
        print(f"  behaviour change found       {len(found)}/{len(verified)}")
        quiet = [r for r in found if r.claim == "no-claim"]
        print(f"  ...on a PR claiming none     {len(quiet)}")
        for r in quiet:
            print(f"      {r.repo}#{r.number}  {r.title[:60]}")
    print(f"\n  spend ${sum(r.cost for r in results):.4f}  "
          f"api calls {budget.calls}/{budget.ceiling}")


if __name__ == "__main__":
    raise SystemExit(main())
