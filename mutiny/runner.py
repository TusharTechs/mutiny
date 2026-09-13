"""Execute pytest inside a target repository and classify each outcome."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .probe import PLUGIN_SOURCE

PASSED = "passed"
FAILED_ASSERTION = "failed_assertion"
ERRORED = "errored"
SKIPPED = "skipped"
NO_TESTS = "no_tests"
# pytest exits 4 on a usage error (an unresolvable node id) and 5 when nothing
# was collected. Neither means the mutant was caught — it means we asked for the
# wrong tests, and treating it as a kill would silently lose real survivors.
SELECTION_ERROR = "selection_error"
_SELECTION_EXITS = {4, 5}


@dataclass(frozen=True)
class TestOutcome:
    nodeid: str
    status: str
    exc_type: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == PASSED


@dataclass(frozen=True)
class PytestRun:
    outcomes: tuple[TestOutcome, ...]
    exit_status: int
    stdout: str
    timed_out: bool = False

    @property
    def verdict(self) -> str:
        """A single status for the run as a whole.

        A proof test is one test, but a generated file may contain helpers, so we
        fold: any error dominates, then any assertion failure, then passed.
        """
        if self.timed_out:
            return ERRORED
        if not self.outcomes:
            return SELECTION_ERROR if self.exit_status in _SELECTION_EXITS else NO_TESTS
        statuses = {o.status for o in self.outcomes}
        # Order matters, and skipped must not dominate. A suite where 362 tests
        # pass and one is skipped is a passing suite; treating it as SKIPPED made
        # `verdict == PASSED` read a healthy run as a failure.
        for dominant in (ERRORED, FAILED_ASSERTION):
            if dominant in statuses:
                return dominant
        return PASSED if PASSED in statuses else SKIPPED

    @property
    def exc_types(self) -> tuple[str, ...]:
        return tuple(o.exc_type for o in self.outcomes if o.exc_type)


def _classify(status: str, exc_type: str | None) -> str:
    if status == "passed":
        return PASSED
    if status == "skipped":
        return SKIPPED
    if status.startswith("error:"):
        return ERRORED
    if status == "failed":
        return FAILED_ASSERTION if exc_type == "AssertionError" else ERRORED
    return ERRORED


def run_pytest(
    root: Path,
    selector: str | Sequence[str],
    python_exe: str = sys.executable,
    timeout: int = 300,
    extra_args: tuple[str, ...] = (),
) -> PytestRun:
    """Run `selector` under pytest in `root` and return classified outcomes.

    `selector` may be one path or node id, or a sequence of them — coverage
    tells us exactly which tests execute a mutated line, and running only those
    is both sound and far cheaper than the whole suite.
    """
    selectors = [selector] if isinstance(selector, str) else list(selector)
    if not selectors:
        raise ValueError("run_pytest needs at least one selector")
    with tempfile.TemporaryDirectory(prefix="mutiny-probe-") as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "mutiny_probe.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
        out_path = plugin_dir / "out.json"

        env = dict(os.environ)
        env["MUTINY_PROBE_OUT"] = str(out_path)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(plugin_dir), env.get("PYTHONPATH", "")) if p
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        cmd = [
            python_exe, "-m", "pytest", *selectors,
            "-p", "mutiny_probe",
            "-p", "no:cacheprovider", "--rootdir", str(root),
            "-q", "--tb=no", "--no-header",
            *extra_args,
        ]
        timed_out = False
        try:
            proc = subprocess.run(
                cmd, cwd=root, env=env, capture_output=True,
                text=True, timeout=timeout,
            )
            stdout, exit_status = proc.stdout + proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            timed_out, stdout, exit_status = True, "TIMEOUT", -1

        outcomes: list[TestOutcome] = []
        if out_path.exists():
            data = json.loads(out_path.read_text())
            for nodeid, rec in data["tests"].items():
                outcomes.append(
                    TestOutcome(nodeid, _classify(rec["status"], rec["exc_type"]), rec["exc_type"])
                )

    return PytestRun(tuple(outcomes), exit_status, stdout, timed_out)
