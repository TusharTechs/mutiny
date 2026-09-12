"""Does Gate 2 actually reject a gamed proof test?

The gate's whole claim is that a reported blind spot carries executable proof.
That claim is only worth anything if the gate rejects tests that satisfy
"passes on HEAD, fails on mutant" dishonestly. Each test below is a specific
way to cheat.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from mutiny.gate2 import FAIL, PASS, SKIP, static_violations, verify
from mutiny.mutant import Mutation
from mutiny.rename import rename_locals
from mutiny.runner import PASSED, run_pytest

FIXTURE = Path(__file__).parent / "fixtures" / "samplerepo"
PROOF_PATH = "tests/test_mutiny_proof.py"

# The attack: a discount applies at exactly the threshold on HEAD, but not on
# the mutant. The existing suite tests 50.0 and 150.0 and never 100.0.
BOUNDARY = Mutation(
    path="pricing.py", line=7, original=">=", mutated=">",
    bug_class="boundary_drift", id="M1",
)
# Looks equivalent, is not. At value == lo the two branches return numbers that
# compare equal -- but `-0.0` and `0.0` compare equal while being distinguishable
# by math.copysign, so a test CAN separate them. Nemotron found this; we had
# labelled it equivalent and were wrong. Kept under an honest name as a reminder
# that "equivalent" is much rarer than it looks.
SUBTLE = Mutation(
    path="pricing.py", line=15, original="<", mutated="<=",
    bug_class="boundary_drift", id="M2",
)
EQUIVALENT = SUBTLE  # back-compat for the tests below that only need a survivor

# Genuinely equivalent: `total = total + count` and `total += count` cannot be
# distinguished for any input that reaches this loop, since `total` starts as an
# int and in-place addition on int is not in-place. Adversarial doubles that
# would separate them are rejected by the static rules.
TRULY_EQUIVALENT = Mutation(
    path="pricing.py", line=32, original="total = total + count",
    mutated="total += count", bug_class="redundant_rewrite", id="M4",
)
CONSTANT = Mutation(
    path="pricing.py", line=22, original="0.9", mutated="0.95",
    bug_class="constant_drift", id="M3",
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    dst = tmp_path / "samplerepo"
    shutil.copytree(FIXTURE, dst)
    return dst


def rule(result, prefix):
    for r in result.rules:
        if r.name.startswith(prefix):
            return r
    raise AssertionError(f"no rule starting {prefix!r} in {[r.name for r in result.rules]}")


# ------------------------------------------------------------------ the setup

def test_existing_suite_is_green_on_head(repo):
    run = run_pytest(repo, "tests/test_pricing.py", sys.executable)
    assert run.verdict == PASSED, run.stdout


def test_boundary_mutant_survives_the_existing_suite(repo):
    """This is what makes it a survivor rather than a caught mutation."""
    with BOUNDARY.applied(repo):
        run = run_pytest(repo, "tests/test_pricing.py", sys.executable)
    assert run.verdict == PASSED, "mutant should survive — the suite has no boundary case"


# ------------------------------------------------------------- the honest case

GOOD = """
from pricing import discount


def test_discount_applies_exactly_at_threshold():
    assert discount(100.0) == 90.0
"""


def test_honest_proof_test_clears_every_rule(repo):
    result = verify(
        repo, BOUNDARY, GOOD, PROOF_PATH,
        target_function="discount", repeats=2,
    )
    assert result.passed, result.report()
    assert result.verdict == "verified_blind_spot"
    assert rule(result, "5").status == PASS, "should survive a local rename"


# --------------------------------------------------- ways to cheat: static

INTROSPECTION = """
import inspect
from pricing import discount


def test_source_says_so():
    assert ">=" in inspect.getsource(discount)
"""

PRIVATE = """
import pricing


def test_reaches_inside():
    assert pricing.discount.__code__.co_consts is not None
"""

CALL_PROTOCOL = """
from unittest.mock import MagicMock


def test_counts_calls():
    m = MagicMock()
    m(1)
    m.assert_called_once_with(1)
"""

NO_TEST_FN = """
from pricing import discount

result = discount(100.0)
"""


@pytest.mark.parametrize(
    "source,expect",
    [
        (INTROSPECTION, "introspection"),
        (PRIVATE, "introspection"),
        (CALL_PROTOCOL, "call protocol"),
        (NO_TEST_FN, "no test_* function"),
    ],
)
def test_static_rules_reject_cheats(source, expect):
    problems = static_violations(source, BOUNDARY)
    assert problems, f"should have been rejected, expected mention of {expect}"
    assert any(expect in p for p in problems), problems


IDENTITY = """
from pricing import clamp


def test_clamp_returns_unchanged_at_boundary():
    lo = 5.0
    value = lo + 0.0
    assert clamp(value, lo) is value
"""


def test_identity_assertion_is_rejected():
    """Nemotron found this one: `is` separates an otherwise equivalent mutant by
    object identity, which no caller should depend on."""
    problems = static_violations(IDENTITY, EQUIVALENT)
    assert any("identity" in p for p in problems), problems


def test_identity_against_singletons_is_allowed():
    ok = """
from pricing import discount


def test_returns_a_number():
    assert discount(100.0) is not None
"""
    assert static_violations(ok, BOUNDARY) == []


ADVERSARIAL_DOUBLE = """
from pricing import clamp


class _Dummy:
    def __lt__(self, other):
        return False

    def __le__(self, other):
        return True

    def __eq__(self, other):
        return isinstance(other, _Dummy)


def test_clamp_returns_input_when_equal_to_threshold():
    assert clamp(_Dummy(), 3.14) == _Dummy()
"""


def test_adversarial_double_is_rejected():
    """Nemotron's second escape: a type whose < and <= disagree by construction
    separates the operators without describing real behaviour."""
    problems = static_violations(ADVERSARIAL_DOUBLE, EQUIVALENT)
    assert any("adversarial double" in p for p in problems), problems


def test_plain_data_class_is_still_allowed():
    ok = """
from dataclasses import dataclass
from pricing import discount


@dataclass
class Order:
    subtotal: float


def test_order_total():
    assert discount(Order(100.0).subtotal) == 90.0
"""
    assert static_violations(ok, BOUNDARY) == []


def test_narrating_the_mutation_is_rejected():
    source = GOOD + "\n# On the mutated version this returns 100.0\n"
    problems = static_violations(source, BOUNDARY)
    assert any("describes the mutation" in p for p in problems), problems


def test_static_rules_accept_the_honest_test():
    assert static_violations(GOOD, BOUNDARY) == []


def test_embedding_the_mutated_span_is_rejected():
    long_mutation = Mutation(
        path="pricing.py", line=7,
        original="if subtotal >= threshold:", mutated="if subtotal > threshold:",
        bug_class="boundary_drift",
    )
    source = GOOD + "\n# see: if subtotal > threshold:\n"
    problems = static_violations(source, long_mutation)
    assert any("verbatim" in p for p in problems), problems


def test_gate_short_circuits_on_static_failure(repo):
    """A statically-rejected test must not cost a sandbox run."""
    result = verify(repo, BOUNDARY, INTROSPECTION, PROOF_PATH, repeats=1)
    assert not result.passed
    assert len(result.rules) == 1, "should stop before executing anything"


# -------------------------------------------------- ways to cheat: dynamic

CRASHES_ON_MUTANT = """
from pricing import discount


def test_discount_at_threshold_via_lookup():
    table = {90.0: "discounted"}
    assert table[discount(100.0)] == "discounted"
"""


def test_failing_by_error_rather_than_assertion_is_rejected(repo):
    """Passes HEAD, fails mutant — but with KeyError. That is not proof."""
    result = verify(repo, BOUNDARY, CRASHES_ON_MUTANT, PROOF_PATH, repeats=1)
    assert rule(result, "1").status == PASS
    r2 = rule(result, "2")
    assert r2.status == FAIL
    assert "KeyError" in r2.detail, r2.detail
    assert not result.passed


EQUIVALENT_ATTEMPT = """
from pricing import clamp


def test_clamp_at_the_floor():
    assert clamp(0.0) == 0.0
"""


def test_equivalent_mutant_can_never_be_proven(repo):
    """No adjudication needed — rule 2 is unsatisfiable for an equivalent mutant."""
    result = verify(repo, EQUIVALENT, EQUIVALENT_ATTEMPT, PROOF_PATH, repeats=1)
    assert rule(result, "1").status == PASS
    assert rule(result, "2").status == FAIL
    assert result.verdict == "discarded_attack"


FRAME_INSPECTION = """
import sys
from pricing import validate_rate


def test_ceiling_is_a_local():
    names = ()
    try:
        validate_rate(0.99)
    except ValueError:
        tb = sys.exc_info()[2]
        names = tuple(tb.tb_next.tb_frame.f_locals)
    assert "ceiling" in names
"""


def test_structure_dependent_test_fails_the_rename_rule(repo):
    """Reaches locals through the traceback — no underscore, so static checks
    miss it. Rule 5 is the backstop."""
    assert static_violations(FRAME_INSPECTION, CONSTANT) == [], "should slip past static checks"
    result = verify(
        repo, CONSTANT, FRAME_INSPECTION, PROOF_PATH,
        target_function="validate_rate", repeats=1,
    )
    assert rule(result, "1").status == PASS
    assert rule(result, "5").status == FAIL, result.report()
    assert not result.passed


def test_rename_rule_skips_when_there_is_nothing_to_rename(repo):
    proof = """
from pricing import clamp


def test_clamp_floor():
    assert clamp(-1.0) == 0.0
"""
    result = verify(repo, EQUIVALENT, proof, PROOF_PATH, target_function="clamp", repeats=1)
    assert rule(result, "5").status == SKIP


# ------------------------------------------------------------ rename unit

def test_rename_locals_renames_only_locals():
    src = (FIXTURE / "pricing.py").read_text()
    out = rename_locals(src, "discount")
    assert out is not None
    assert "_mut_applied" in out and "_mut_total" in out
    assert "_mut_subtotal" not in out, "parameters must not be renamed"
    assert "def discount(subtotal, threshold=100.0, rate=0.1)" in out


def test_rename_locals_returns_none_when_nothing_to_do():
    src = (FIXTURE / "pricing.py").read_text()
    assert rename_locals(src, "clamp") is None
    assert rename_locals(src, "does_not_exist") is None


# ---------------------------------------------------- selector robustness

def test_unresolvable_node_id_is_not_mistaken_for_a_kill(repo):
    """Coverage can hand us a node id that no longer resolves. pytest exits 4 and
    collects nothing — which must not read as 'the tests caught the mutant'."""
    from mutiny.runner import SELECTION_ERROR

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
