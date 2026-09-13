"""The test runner has to distinguish a bad selection from a real result.

Coverage can hand us a node id that no longer resolves. pytest then exits 4 and
collects nothing, and anything that is not PASSED used to read as "the tests
caught it" — which silently destroys real findings.
"""
import shutil
import sys
from pathlib import Path

import pytest

from mutiny.runner import PASSED, SELECTION_ERROR, run_pytest

FIXTURE = Path(__file__).parent / "fixtures" / "samplerepo"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dst = tmp_path / "samplerepo"
    shutil.copytree(FIXTURE, dst)
    return dst


def test_green_suite_passes(repo):
    assert run_pytest(repo, "tests/test_pricing.py", sys.executable).verdict == PASSED


def test_unresolvable_node_id_is_not_mistaken_for_a_kill(repo):
    run = run_pytest(repo, ["tests/test_pricing.py::test_does_not_exist"], sys.executable)
    assert run.verdict == SELECTION_ERROR
    assert run.verdict != PASSED


def test_multiple_node_ids_run_together(repo):
    run = run_pytest(
        repo,
        ["tests/test_pricing.py::test_small_order_gets_no_discount",
         "tests/test_pricing.py::test_clamp_floors_negatives"],
        sys.executable,
    )
    assert run.verdict == PASSED
    assert len(run.outcomes) == 2


def test_assertion_failure_is_distinct_from_an_error(repo):
    """Gate-era machinery is gone, but this distinction still matters: a test
    that errors has not demonstrated anything about behaviour."""
    proof = repo / "tests" / "test_tmp.py"
    proof.write_text("def test_asserts():\n    assert 1 == 2\n")
    run = run_pytest(repo, "tests/test_tmp.py", sys.executable)
    assert run.verdict == "failed_assertion"
    assert "AssertionError" in run.exc_types

    proof.write_text("def test_errors():\n    raise KeyError('boom')\n")
    run = run_pytest(repo, "tests/test_tmp.py", sys.executable)
    assert run.verdict == "errored"
