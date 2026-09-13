#!/usr/bin/env python
"""What happens when the test suite is the size most suites actually are?

The refactor benchmark on semver, cachetools and packaging produced a number
that argues against this project: of the rewrites their suites accepted, none
changed behaviour. On those repositories the tests caught everything and MUTINY
added nothing.

That result is real, and it is also unrepresentative. Those are among the best
tested libraries in the language — packaging runs 62,000 tests. Sampling more
public repositories does not fix it: schedule measures 94% statement coverage,
shortuuid 91%, validators 88%. Well-tested code is what gets published; the
thinly covered code is private, and we cannot sample it.

So this asks the question directly instead. For every rewrite, the full suite
judges it, and so does a random subset of the test files — a project with a
third of the tests, which is an ordinary state of affairs. The interesting
population is the rewrites the subset accepts and the full suite rejects: real
breakage that a normal suite would have missed. For those, does the differential
catch it?

Subsets are drawn with a fixed seed and reported, so the sampling is
reproducible rather than convenient.
"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import refactor as refactor_mod
from mutiny.coverage import measure
from mutiny.differential import compare, confirm
from mutiny.differential import observe as local_observe
from mutiny.inputs import generate_validated, is_stateful, receiver_candidates
from mutiny.models import BudgetExceeded, NemotronClient
from mutiny.runner import PASSED, SELECTION_ERROR, run_pytest
from mutiny.source import focused_module, function_span

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT.parent / "checkouts"
SEED = 20260913
SUBSET_FRACTION = 0.34
SUBSET_DRAWS = 3
N_INPUTS = 30

# (repo, module, functions worth rewriting)
TARGETS = [
    ("python-semver", "semver.version",
     ["Version.next_version", "Version.compare", "Version.match", "Version.bump_build"]),
    ("cachetools", "cachetools",
     ["Cache.__setitem__", "LRUCache.popitem", "TTLCache.expire", "LFUCache.__setitem__"]),
    ("schedule", "schedule",
     ["Job.do", "Job._schedule_next_run", "Scheduler.run_pending", "Job.at"]),
    ("shortuuid", "shortuuid.main",
     ["ShortUUID.encode", "ShortUUID.decode", "ShortUUID.random"]),
]


@dataclass
class Case:
    repo: str
    function: str
    refactored: bool = False
    full_suite_passes: bool | None = None
    subsets_passing: int = 0
    subsets_total: int = 0
    divergent: int = 0
    probes: int = 0
    witnesses: list = field(default_factory=list)
    error: str = ""

    @property
    def slipped(self) -> bool:
        """Broken, but a reduced suite let it through."""
        return self.full_suite_passes is False and self.subsets_passing > 0

    @property
    def caught_by_us(self) -> bool:
        return self.divergent > 0


def test_files(repo: Path) -> list[str]:
    """Every file that looks like a test, wherever the project keeps them.

    Layouts vary more than expected: tests/ directories, a single tests.py, a
    test_x.py beside the source. Missing them means no subsets to draw, which
    quietly removes a repository from the experiment.
    """
    out: list[str] = []
    skip = {".git", ".venv", "venv", "build", "dist", "node_modules"}
    for path in repo.rglob("*.py"):
        rel = path.relative_to(repo)
        if skip & set(rel.parts):
            continue
        name = rel.name
        if (name.startswith("test_") or name.endswith("_test.py")
                or name in {"tests.py", "test.py"} or "tests" in rel.parts[:-1]):
            if name != "__init__.py" and not name.startswith("conftest"):
                out.append(str(rel))
    return sorted(set(out))


def subsets(files: list[str], rng: random.Random) -> list[list[str]]:
    """Random draws of roughly a third of the test files."""
    if len(files) <= 1:
        return []
    size = max(1, round(len(files) * SUBSET_FRACTION))
    return [rng.sample(files, size) for _ in range(SUBSET_DRAWS)]


def restore(repo: Path, path: str) -> None:
    subprocess.run(["git", "checkout", "-q", "--", path], cwd=repo, capture_output=True)


def run_case(client, repo: Path, repo_name: str, module: str,
             qualname: str, python_exe: str, rng: random.Random) -> Case:
    case = Case(repo=repo_name, function=qualname)
    located = _find(repo, qualname)
    if located is None:
        case.error = "function not found"
        return case
    path, rel = located
    source = path.read_text(encoding="utf-8")

    try:
        rewrite = refactor_mod.refactor(client, source, qualname)
        if rewrite is None or not rewrite.changed:
            case.error = "no usable rewrite"
            return case
        patched = refactor_mod.apply(source, qualname, rewrite.rewritten)
        if patched is None:
            case.error = "rewrite did not apply"
            return case
        case.refactored = True

        files = test_files(repo)
        draws = subsets(files, rng)
        case.subsets_total = len(draws)

        path.write_text(patched, encoding="utf-8")
        try:
            full = run_pytest(repo, "tests/" if (repo / "tests").is_dir() else files,
                              python_exe, timeout=900)
            case.full_suite_passes = full.verdict == PASSED
            for draw in draws:
                sub = run_pytest(repo, draw, python_exe, timeout=900)
                if sub.verdict in (PASSED, SELECTION_ERROR):
                    case.subsets_passing += 1
        finally:
            restore(repo, rel)

        # Probe regardless: the comparison is only meaningful alongside the suites.
        cov = measure(repo, "tests/" if (repo / "tests").is_dir() else files,
                      module.split(".")[0], python_exe)
        examples = []
        if cov.measured:
            from mutiny.coverage import covering_examples
            lo, _ = function_span(source, qualname)
            examples = covering_examples(repo, cov, rel, lo, limit=2)

        probes, _ = generate_validated(
            client, module, qualname, focused_module(source, qualname),
            probe=lambda e: local_observe(repo, module, e, python_exe),
            n=N_INPUTS, covering_tests=examples,
            subclasses=receiver_candidates(source, qualname),
            stateful=is_stateful(source, qualname),
            diff="\n".join(__import__("difflib").unified_diff(
                rewrite.original.splitlines(), rewrite.rewritten.splitlines(),
                lineterm="", n=4)),
        )
        case.probes = len(probes)
        if not probes:
            return case

        before = local_observe(repo, module, probes, python_exe)
        path.write_text(patched, encoding="utf-8")
        try:
            after = local_observe(repo, module, probes, python_exe, baseline=False)
        finally:
            restore(repo, rel)

        divergences, _ = confirm(
            compare(before, after),
            run_before=lambda e: local_observe(repo, module, e, python_exe),
            run_after=lambda e: _with_patch(repo, rel, patched, module, e, python_exe),
        )
        case.divergent = len(divergences)
        case.witnesses = [str(d) for d in divergences[:2]]
    except BudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        case.error = f"{type(exc).__name__}: {exc}"[:150]
    finally:
        restore(repo, rel)
    return case


def _with_patch(repo, rel, patched, module, exprs, python_exe):
    original = (repo / rel).read_bytes()
    (repo / rel).write_text(patched, encoding="utf-8")
    try:
        return local_observe(repo, module, exprs, python_exe, baseline=False)
    finally:
        (repo / rel).write_bytes(original)


def _find(repo: Path, qualname: str):
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if {".git", ".venv", "tests"} & set(rel.parts) or rel.name.startswith("test_"):
            continue
        try:
            function_span(path.read_text(encoding="utf-8"), qualname)
        except (ValueError, SyntaxError, UnicodeDecodeError, OSError):
            continue
        return path, str(rel)
    return None


def main() -> int:
    client = NemotronClient(cap_usd=12.0)
    opening = client.ledger.total_usd
    rng = random.Random(SEED)
    cases: list[Case] = []
    out = ROOT / "results" / f"thin-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    for repo_name, module, functions in TARGETS:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        if not Path(python_exe).exists():
            print(f"  {repo_name}: no venv, skipping")
            continue
        print(f"\n{repo_name}  ({len(test_files(repo))} test files)")
        for qualname in functions:
            try:
                case = run_case(client, repo, repo_name, module, qualname, python_exe, rng)
            except BudgetExceeded as exc:
                print(f"  BUDGET: {exc}")
                break
            cases.append(case)
            bits = []
            if case.full_suite_passes is False:
                bits.append("full suite REJECTS")
            elif case.full_suite_passes:
                bits.append("full suite accepts")
            if case.subsets_total:
                bits.append(f"{case.subsets_passing}/{case.subsets_total} subsets accept")
            bits.append(f"{case.divergent} divergent of {case.probes}")
            print(f"  {qualname:28s} {'; '.join(bits)}"
                  + (f"  [{case.error}]" if case.error else ""))
            out.write_text(json.dumps([asdict(c) for c in cases], indent=2))

    print(f"\n\n{'=' * 74}\nTHIN-SUITE BENCHMARK\n{'=' * 74}")
    produced = [c for c in cases if c.refactored]
    broken = [c for c in produced if c.full_suite_passes is False]
    slipped = [c for c in broken if c.slipped]
    caught = [c for c in slipped if c.caught_by_us]
    accepted = [c for c in produced if c.full_suite_passes]
    noisy = [c for c in accepted if c.caught_by_us]

    print(f"  rewrites produced                        {len(produced)}/{len(cases)}")
    if produced:
        print(f"  broken (full suite rejects)              {len(broken)}/{len(produced)}"
              f" = {100 * len(broken) / len(produced):.0f}%")
    if broken:
        print(f"  of those, a {int(SUBSET_FRACTION*100)}% suite would have missed  "
              f"{len(slipped)}/{len(broken)}")
    if slipped:
        print(f"    MUTINY caught                          {len(caught)}/{len(slipped)}"
              f" = {100 * len(caught) / len(slipped):.0f}%   <- the case for the tool")
    if accepted:
        print(f"  rewrites the full suite accepted         {len(accepted)}")
        print(f"    MUTINY flagged anyway                  {len(noisy)}"
              f"   (inspect: real subtle break, or false alarm)")
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
