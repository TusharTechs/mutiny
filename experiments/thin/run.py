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
N_INPUTS = 30

def discover_targets(repo: Path, limit: int = 14) -> list[tuple[str, str]]:
    """Functions worth rewriting, chosen by shape rather than by hand.

    Hand-picking targets puts the experimenter inside the measurement. The rule
    here is mechanical: a function with real branching, in a source file, not a
    trivial accessor, that resolves unambiguously.
    """
    import ast

    found: list[tuple[int, str, str]] = []
    skip = {".git", ".venv", "tests", "test", "build", "dist", "docs"}
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if skip & set(rel.parts) or rel.name.startswith(("test_", "setup", "conf")):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        module = module_name(str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.append(_score(child, f"{node.name}.{child.name}", module))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(_score(node, node.name, module))

    ranked = sorted((f for f in found if f[0] > 0), key=lambda f: -f[0])
    out, seen = [], set()
    for _score_value, qualname, module in ranked:
        if qualname in seen:
            continue
        seen.add(qualname)
        out.append((module, qualname))
        if len(out) >= limit:
            break
    return out


def _score(node, qualname: str, module: str) -> tuple[int, str, str]:
    import ast

    body = list(ast.walk(node))
    branches = sum(1 for n in body if isinstance(n, (ast.If, ast.For, ast.While, ast.Try)))
    returns = sum(1 for n in body if isinstance(n, ast.Return) and n.value)
    lines = (node.end_lineno or node.lineno) - node.lineno
    if lines < 5 or lines > 70 or branches == 0 or node.name.startswith("__init__"):
        return (0, qualname, module)
    return (branches * 2 + returns + min(lines, 40) // 10, qualname, module)


def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


REPOS = ["python-semver", "cachetools", "schedule", "shortuuid", "pytimeparse"]
PER_REPO = 14
SUBSET_DRAWS = 2

TARGETS_MANUAL = [
    ("python-semver", "semver.version",
     ["Version.next_version", "Version.compare", "Version.match", "Version.bump_build",
      "Version.bump_prerelease", "Version.replace", "Version.parse"]),
    ("cachetools", "cachetools",
     ["Cache.__setitem__", "LRUCache.popitem", "TTLCache.expire", "LFUCache.__setitem__",
      "FIFOCache.__setitem__", "Cache.__delitem__", "TLRUCache.__setitem__"]),
    ("schedule", "schedule",
     ["Job.do", "Job._schedule_next_run", "Scheduler.run_pending", "Job.at",
      "Job.to", "Scheduler.get_jobs", "Job.tag"]),
    ("shortuuid", "shortuuid.main",
     ["ShortUUID.encode", "ShortUUID.decode", "ShortUUID.random",
      "ShortUUID.set_alphabet", "ShortUUID.uuid"]),
]


@dataclass
class Case:
    repo: str
    function: str
    refactored: bool = False
    full_suite_passes: bool | None = None
    subsets_passing: int = 0
    subsets_total: int = 0
    suite_size: int = 0
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


def collect(repo: Path, python_exe: str, selector) -> list[str]:
    """Every test node id the suite contains.

    Sampling whole files cannot model schedule or shortuuid, which keep their
    entire suite in one file — the draw is either everything or nothing. Node ids
    are the unit a suite actually grows in.
    """
    sel = [selector] if isinstance(selector, str) else list(selector)
    out = subprocess.run(
        [python_exe, "-m", "pytest", *sel, "--collect-only", "-q",
         "-p", "no:cacheprovider", "--rootdir", str(repo)],
        cwd=repo, capture_output=True, text=True, timeout=600).stdout
    return [line.strip() for line in out.splitlines()
            if "::" in line and not line.startswith(("=", "<", " "))]


def subsets(nodes: list[str], rng: random.Random) -> list[list[str]]:
    """Random draws of roughly a third of the tests."""
    if len(nodes) < 4:
        return []
    size = max(1, round(len(nodes) * SUBSET_FRACTION))
    return [rng.sample(nodes, size) for _ in range(SUBSET_DRAWS)]


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
        selector = "tests/" if (repo / "tests").is_dir() else files
        nodes = collect(repo, python_exe, selector)
        draws = subsets(nodes, rng)
        case.subsets_total = len(draws)
        case.suite_size = len(nodes)

        path.write_text(patched, encoding="utf-8")
        try:
            full = run_pytest(repo, selector, python_exe, timeout=900)
            case.full_suite_passes = full.verdict == PASSED
            for draw in draws:
                sub = run_pytest(repo, draw, python_exe, timeout=900)
                if sub.verdict in (PASSED, SELECTION_ERROR):
                    case.subsets_passing += 1
        finally:
            restore(repo, rel)

        # Probe regardless: the comparison is only meaningful alongside the suites.
        cov = measure(repo, selector, module.split(".")[0], python_exe)
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

    for repo_name in REPOS:
        repo = CHECKOUTS / repo_name
        python_exe = str(repo / ".venv" / "bin" / "python")
        if not Path(python_exe).exists():
            print(f"  {repo_name}: no venv, skipping")
            continue
        discovered = discover_targets(repo, PER_REPO)
        print(f"\n{repo_name}  ({len(test_files(repo))} test files, "
              f"{len(discovered)} targets)")
        for module, qualname in discovered:
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
