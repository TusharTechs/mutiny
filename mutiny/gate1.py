"""Gate 1 — plausibility.

A mutant that will not import, or that the repo's own linter or type checker
rejects instantly, is a broken build rather than a blind spot. Reporting one
destroys trust, and executing one wastes a run. This gate is cheap by design:
it must cost far less than the test suite it protects.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .mutant import Mutation, MutationError


@dataclass(frozen=True)
class Plausibility:
    ok: bool
    reason: str = ""


def _syntax_ok(source: str) -> tuple[bool, str]:
    try:
        ast.parse(source)
    except SyntaxError as exc:
        return False, f"does not parse: {exc.msg} at line {exc.lineno}"
    return True, ""


class _StripAnnotations(ast.NodeTransformer):
    """Erase every annotation, which Python does not evaluate at runtime."""

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node):  # type: ignore[no-untyped-def]
        node.returns = None
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign):  # type: ignore[no-untyped-def]
        if node.value is None:
            return None
        return ast.Assign(targets=[node.target], value=node.value)


def _semantically_identical(before: str, after: str) -> bool:
    """True when the two sources differ only in things Python never executes.

    Annotations are the case that matters: `SupportsInt -> SupportsFloat` in a
    signature reads like a real change and cannot alter behaviour, so no test can
    ever distinguish it. Caught here it costs nothing; caught at Gate 2 it costs
    a full suite run and three proof-test attempts.
    """
    try:
        trees = [
            ast.dump(ast.fix_missing_locations(_StripAnnotations().visit(ast.parse(src))))
            for src in (before, after)
        ]
    except SyntaxError:
        return False
    return trees[0] == trees[1]


def _touches_only_comment(mutation: Mutation, source: str) -> bool:
    """A change confined to a string or comment cannot alter behaviour."""
    line = source.splitlines()[mutation.line - 1]
    stripped = line.strip()
    return stripped.startswith("#") or (
        mutation.original in stripped
        and stripped.startswith(('"""', "'''", '"', "'"))
    )


def check(
    mutation: Mutation,
    repo: Path,
    python_exe: str | None = None,
    run_linters: bool = True,
) -> Plausibility:
    python_exe = python_exe or sys.executable
    try:
        patched = mutation._patched_source(repo)
    except MutationError as exc:
        return Plausibility(False, str(exc).splitlines()[0])

    original = (repo / mutation.path).read_text(encoding="utf-8")
    if _touches_only_comment(mutation, original):
        return Plausibility(False, "changes only a comment or string literal")

    ok, why = _syntax_ok(patched)
    if not ok:
        return Plausibility(False, why)

    if _semantically_identical(original, patched):
        return Plausibility(False, "changes only annotations or other non-executed code")

    with mutation.applied(repo):
        module = mutation.path
        proc = subprocess.run(
            [python_exe, "-c", f"import ast,sys; ast.parse(open({module!r}).read())"],
            cwd=repo, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            return Plausibility(False, "fails to parse in target interpreter")

        if run_linters and shutil.which("ruff"):
            lint = subprocess.run(
                ["ruff", "check", "--quiet", "--select", "E9,F", mutation.path],
                cwd=repo, capture_output=True, text=True, timeout=120,
            )
            if lint.returncode != 0 and lint.stdout.strip():
                first = lint.stdout.strip().splitlines()[0]
                return Plausibility(False, f"ruff rejects it: {first[:120]}")

    return Plausibility(True)
