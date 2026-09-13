"""Which existing tests execute this line?

Two things depend on the answer.

A mutation on a line no test runs is not a blind spot — it is uncovered code,
which coverage.py reports for free. The finding worth having is a line that *is*
covered and still unconstrained, so Gate 1 needs coverage to tell those apart.

And the proof-test generator keeps failing for want of exactly this. Given a
mutation inside an internal function it cannot see how a caller reaches the line,
so it writes a reasonable test of an adjacent public function that the mutation
never touches. The tests that already execute the line are a worked example of
how to get there — the hardest inference in the task, available for free from a
single instrumented run of the suite.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CoverageMap:
    """Line -> the test ids that executed it, for one repository at one commit."""

    by_line: dict[tuple[str, int], tuple[str, ...]] = field(default_factory=dict)
    measured: bool = False
    note: str = ""

    def tests_for(self, path: str, line: int, limit: int = 4) -> tuple[str, ...]:
        return self.by_line.get((path, line), ())[:limit]

    def is_covered(self, path: str, line: int) -> bool:
        return bool(self.by_line.get((path, line)))

    def covered_lines(self, path: str) -> set[int]:
        return {ln for (p, ln) in self.by_line if p == path}


def _strip_context(context: str) -> str:
    """`tests/test_x.py::test_y|run` -> `tests/test_x.py::test_y`."""
    return context.split("|", 1)[0].strip()


def _cache_path(repo: Path, selector: str, package: str) -> Path | None:
    """Coverage for a given commit never changes, so measuring it twice is waste."""
    import hashlib
    import subprocess as sp

    head = sp.run(["git", "rev-parse", "HEAD"], cwd=repo,
                  capture_output=True, text=True).stdout.strip()
    if not head:
        return None
    key = hashlib.sha256(f"{head}|{selector}|{package}".encode()).hexdigest()[:24]
    return Path(__file__).resolve().parent.parent / ".cache" / "coverage" / f"{key}.json"


def measure(
    repo: Path,
    selector: str | Sequence[str],
    package: str,
    python_exe: str | None = None,
    timeout: int = 1800,
    use_cache: bool = True,
) -> CoverageMap:
    """Run the suite once with per-test contexts and map every line to its tests."""
    python_exe = python_exe or sys.executable
    # Layouts without a tests/ directory are given a list of files instead, and
    # passing that straight into the command line fails far from the cause.
    selectors = [selector] if isinstance(selector, str) else list(selector)
    if not selectors:
        return CoverageMap(note="no test selector given")

    cache = _cache_path(repo, "|".join(selectors), package) if use_cache else None
    if cache and cache.is_file():
        raw = json.loads(cache.read_text())
        return CoverageMap(
            by_line={(p, int(ln)): tuple(t) for p, ln, t in raw["by_line"]},
            measured=raw["measured"], note=raw.get("note", ""),
        )

    with tempfile.TemporaryDirectory(prefix="mutiny-cov-") as tmp:
        data_file = Path(tmp) / "cov.sqlite"
        env = dict(os.environ)
        env["COVERAGE_FILE"] = str(data_file)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        proc = subprocess.run(
            [
                python_exe, "-m", "pytest", *selectors,
                f"--cov={package}", "--cov-context=test", "--cov-report=",
                "-p", "no:cacheprovider", "--rootdir", str(repo), "-q", "--tb=no", "--no-header",
            ],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if not data_file.exists():
            return CoverageMap(
                note=f"no coverage data produced: {(proc.stdout + proc.stderr).strip()[-200:]}"
            )

        try:
            from coverage import CoverageData
        except ImportError:
            return CoverageMap(note="coverage is not installed in the target environment")

        data = CoverageData(basename=str(data_file))
        data.read()

        by_line: dict[tuple[str, int], tuple[str, ...]] = {}
        for abs_path in data.measured_files():
            try:
                rel = str(Path(abs_path).resolve().relative_to(repo.resolve()))
            except ValueError:
                continue  # outside the repo: site-packages, stdlib
            for lineno, contexts in (data.contexts_by_lineno(abs_path) or {}).items():
                tests = tuple(
                    sorted({_strip_context(c) for c in contexts if c and c.strip()})
                )
                if tests:
                    by_line[(rel, lineno)] = tests

    result = CoverageMap(by_line=by_line, measured=True)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "measured": True,
            "by_line": [[p, ln, list(t)] for (p, ln), t in by_line.items()],
        }))
    return result


def test_source(repo: Path, test_id: str) -> str | None:
    """Source of the test function named by `path::[Class::]name`."""
    parts = test_id.split("::")
    if len(parts) < 2:
        return None
    rel, name = parts[0], parts[-1]
    name = name.split("[", 1)[0]  # drop a parametrise suffix
    target = repo / rel
    if not target.is_file():
        return None
    try:
        text = target.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            lines = text.splitlines()
            lo = min(node.lineno, *(d.lineno for d in node.decorator_list)) if node.decorator_list else node.lineno
            return "\n".join(lines[lo - 1 : node.end_lineno])
    return None


def covering_examples(
    repo: Path, coverage: CoverageMap, path: str, line: int, limit: int = 3
) -> list[tuple[str, str]]:
    """(test id, source) for up to `limit` tests that execute this line."""
    out: list[tuple[str, str]] = []
    for test_id in coverage.tests_for(path, line, limit=limit * 2):
        src = test_source(repo, test_id)
        if src:
            out.append((test_id, src))
        if len(out) >= limit:
            break
    return out
