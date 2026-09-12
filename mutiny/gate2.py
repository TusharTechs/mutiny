"""Gate 2 — proof-test verification.

A surviving mutant becomes a reported blind spot only if MUTINY can produce a
test that satisfies every rule below. "Passes on HEAD, fails on the mutant" is
necessary but not sufficient: it is satisfiable by a test that pins an
implementation detail, reads the source, or reimplements the function. The
remaining rules exist to make that class of test fail the gate.

Because a behaviourally equivalent mutation cannot be distinguished by any test,
equivalent mutants can never satisfy rule 2 — so they are discarded by the same
mechanism that produces the evidence, and never need adjudicating.
"""
from __future__ import annotations

import ast
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .mutant import Mutation
from .rename import rename_locals
from .runner import FAILED_ASSERTION, PASSED, PytestRun, run_pytest

PASS, FAIL, SKIP = "pass", "fail", "skip"

FORBIDDEN_IMPORTS = {
    "inspect", "dis", "marshal", "linecache", "ast", "ctypes", "gc", "py_compile",
}
FORBIDDEN_ATTRS = {
    "__code__", "__wrapped__", "__globals__", "__func__", "__closure__",
    "co_code", "co_consts", "_getframe", "__subclasshook__",
}
# `x is y` on non-singletons asserts on object identity, which is an
# implementation detail no caller should depend on -- and a reliable way to
# distinguish a mutant that is otherwise behaviourally equivalent.
IDENTITY_SINGLETONS = {None, True, False}

# A test that defines its own type overriding comparison or coercion can
# separate almost any mutation -- an object whose __lt__ is always False and
# whose __le__ is always True distinguishes `<` from `<=` without saying
# anything about how the function behaves for the inputs it is written for.
ADVERSARIAL_DUNDERS = {
    "__lt__", "__le__", "__gt__", "__ge__", "__eq__", "__ne__", "__hash__",
    "__bool__", "__len__", "__index__", "__int__", "__float__", "__round__",
    "__add__", "__sub__", "__mul__", "__truediv__", "__floordiv__", "__mod__",
    "__contains__", "__iter__", "__getitem__",
}

# A proof test should read as a test someone would have written anyway, so prose
# about "the mutant" is a tell. Keep this narrow: "original implementation" was
# in here and rejected a perfectly good test whose comment explained what the
# function is supposed to do. Only phrases that can only mean "I am describing a
# diff" belong here.
MUTATION_TELLS = ("mutant", "mutated version", "mutated implementation",
                  "the mutation", "before the mutation", "after the mutation")

PROTOCOL_ASSERT_ATTRS = {
    "assert_called", "assert_called_once", "assert_called_with",
    "assert_called_once_with", "assert_has_calls", "assert_any_call",
    "assert_not_called", "call_count", "call_args", "call_args_list", "mock_calls",
}


@dataclass(frozen=True)
class RuleResult:
    name: str
    status: str
    detail: str = ""

    def __str__(self) -> str:
        mark = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[self.status]
        return f"[{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass(frozen=True)
class GateResult:
    rules: tuple[RuleResult, ...]

    @property
    def passed(self) -> bool:
        return all(r.status != FAIL for r in self.rules)

    @property
    def verdict(self) -> str:
        return "verified_blind_spot" if self.passed else "discarded_attack"

    @property
    def failures(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rules if r.status == FAIL)

    def report(self) -> str:
        head = f"{self.verdict.upper().replace('_', ' ')}"
        return "\n".join([head, *(f"  {r}" for r in self.rules)])


# ---------------------------------------------------------------- static rules

def _dotted_is_private(name: str) -> bool:
    return any(part.startswith("_") for part in name.split("."))


def static_violations(
    source: str, mutation: Mutation, allow_protocol_assertions: bool = False
) -> list[str]:
    """Rules 3 and 4, checked without executing anything."""
    problems: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"proof test does not parse: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_mod = alias.name.split(".")[0]
                if root_mod in FORBIDDEN_IMPORTS:
                    problems.append(f"imports {alias.name!r} (introspection)")
                elif _dotted_is_private(alias.name):
                    problems.append(f"imports private module {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root_mod = mod.split(".")[0]
            if root_mod in FORBIDDEN_IMPORTS:
                problems.append(f"imports from {mod!r} (introspection)")
            elif _dotted_is_private(mod):
                problems.append(f"imports from private module {mod!r}")
            for alias in node.names:
                if alias.name.startswith("_") and not alias.name.startswith("__"):
                    problems.append(f"imports private name {alias.name!r}")
        elif isinstance(node, ast.Compare):
            if not allow_protocol_assertions:
                for op, right in zip(node.ops, node.comparators):
                    if not isinstance(op, (ast.Is, ast.IsNot)):
                        continue
                    operands = (node.left, right)
                    if any(
                        isinstance(o, ast.Constant) and o.value in IDENTITY_SINGLETONS
                        for o in operands
                    ):
                        continue  # `x is None` and friends are fine
                    problems.append(
                        "compares object identity with `is` rather than value — "
                        "identity is an implementation detail"
                    )
        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr in FORBIDDEN_ATTRS:
                problems.append(f"accesses {attr!r} (introspection)")
            elif attr.startswith("_") and not (attr.startswith("__") and attr.endswith("__")):
                problems.append(f"accesses private attribute {attr!r}")
            elif attr in PROTOCOL_ASSERT_ATTRS and not allow_protocol_assertions:
                problems.append(
                    f"asserts on call protocol via {attr!r} rather than on a value"
                )

    # The proof test must demonstrate behaviour, not quote the mutation. Short
    # spans (an operator, a digit) are skipped — they collide with ordinary code.
    for span, label in ((mutation.mutated, "mutated"), (mutation.original, "original")):
        needle = span.strip()
        if len(needle) >= 12 and needle in source:
            problems.append(f"embeds the {label} source span verbatim")

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        overridden = {
            child.name
            for child in node.body
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        } & ADVERSARIAL_DUNDERS
        if overridden:
            problems.append(
                f"defines {node.name!r} overriding {', '.join(sorted(overridden))} — "
                "an adversarial double, not an input the function is written for"
            )

    lowered = source.lower()
    for tell in MUTATION_TELLS:
        if tell in lowered:
            problems.append(f"describes the mutation ({tell!r}) instead of the behaviour")
            break

    if not any(
        isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
        for n in ast.walk(tree)
    ):
        problems.append("contains no test_* function")

    return problems


# --------------------------------------------------------------- dynamic rules

@contextlib.contextmanager
def _file_installed(root: Path, rel_path: str, source: str) -> Iterator[Path]:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    backup = target.read_text(encoding="utf-8") if existed else None
    target.write_text(source, encoding="utf-8")
    try:
        yield target
    finally:
        if backup is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(backup, encoding="utf-8")


def _batch(
    root: Path, selector: str, python_exe: str, repeats: int, timeout: int
) -> list[PytestRun]:
    return [run_pytest(root, selector, python_exe, timeout) for _ in range(repeats)]


def _verdicts(runs: list[PytestRun]) -> list[str]:
    return [r.verdict for r in runs]


def verify(
    root: Path,
    mutation: Mutation,
    proof_test_source: str,
    proof_test_path: str,
    target_function: str | None = None,
    python_exe: str = sys.executable,
    repeats: int = 3,
    timeout: int = 300,
    allow_protocol_assertions: bool = False,
) -> GateResult:
    """Run every Gate 2 rule against one candidate proof test."""
    rules: list[RuleResult] = []

    # Rules 3 & 4 first: they cost nothing and reject the cheapest fakes.
    problems = static_violations(proof_test_source, mutation, allow_protocol_assertions)
    rules.append(
        RuleResult(
            "3+4 public interface, asserts on values",
            FAIL if problems else PASS,
            "; ".join(problems),
        )
    )
    if problems:
        return GateResult(tuple(rules))

    # Rule 1 — passes on unmodified HEAD.
    with _file_installed(root, proof_test_path, proof_test_source):
        head_runs = _batch(root, proof_test_path, python_exe, repeats, timeout)
    head_verdicts = _verdicts(head_runs)
    head_ok = all(v == PASSED for v in head_verdicts)
    rules.append(
        RuleResult(
            "1 passes on HEAD",
            PASS if head_ok else FAIL,
            "" if head_ok else f"verdicts={head_verdicts}; {head_runs[0].stdout.strip()[-300:]}",
        )
    )

    # Rule 2 — fails on the mutant, by assertion.
    with mutation.applied(root):
        with _file_installed(root, proof_test_path, proof_test_source):
            mutant_runs = _batch(root, proof_test_path, python_exe, repeats, timeout)
    mutant_verdicts = _verdicts(mutant_runs)
    mutant_ok = all(v == FAILED_ASSERTION for v in mutant_verdicts)
    detail = ""
    if not mutant_ok:
        excs = mutant_runs[0].exc_types
        detail = f"verdicts={mutant_verdicts}"
        if excs:
            detail += f", raised {excs[0]} rather than AssertionError"
    rules.append(RuleResult("2 fails on mutant by assertion", PASS if mutant_ok else FAIL, detail))

    # Rule 6 — deterministic across repeats.
    stable = len(set(head_verdicts)) == 1 and len(set(mutant_verdicts)) == 1
    rules.append(
        RuleResult(
            "6 deterministic",
            PASS if stable else FAIL,
            "" if stable else f"HEAD={head_verdicts} MUTANT={mutant_verdicts}",
        )
    )

    # Rule 5 — invariant to a semantics-preserving rename of the target's locals.
    # Only meaningful for a test that passes on HEAD: one that does not would fail
    # the renamed run too, and reporting that as a second, separate defect
    # overstates how many distinct things went wrong.
    if not head_ok:
        rules.append(RuleResult("5 rename-invariant", SKIP, "test does not pass on HEAD"))
    elif target_function is None:
        rules.append(RuleResult("5 rename-invariant", SKIP, "no target function supplied"))
    else:
        original = (root / mutation.path).read_text(encoding="utf-8")
        renamed = rename_locals(original, target_function)
        if renamed is None:
            rules.append(
                RuleResult("5 rename-invariant", SKIP, f"no renameable locals in {target_function}")
            )
        else:
            with _file_installed(root, mutation.path, renamed):
                with _file_installed(root, proof_test_path, proof_test_source):
                    run = run_pytest(root, proof_test_path, python_exe, timeout)
            ok = run.verdict == PASSED
            rules.append(
                RuleResult(
                    "5 rename-invariant",
                    PASS if ok else FAIL,
                    "" if ok else f"broke under rename (verdict={run.verdict}) — "
                    "test encodes structure, not behaviour",
                )
            )

    return GateResult(tuple(rules))
