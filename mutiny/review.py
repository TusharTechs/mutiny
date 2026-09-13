"""Review a change that already exists — a branch, a pull request, a commit.

`mutiny verify` invents a rewrite and checks it. This checks one you were
already going to merge, which is the shape the tool is actually for.

Nothing is checked out. `git archive` and `git show` read any revision straight
out of the object store, so the developer's working copy is untouched and CI
jobs sharing a clone do not fight each other.

One checkpoint is warmed at the base revision. Each fork then has the head
version of the changed files laid over the top, so the two sides of the diff run
from the same expensive setup and never see one another.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .diff import (ChangedFile, changed_between, changed_lines,
                   enclosing_functions, hunk_between)
from .source import function_span


@dataclass(frozen=True)
class Target:
    """One function a change touched, and enough context to probe it."""

    path: str
    qualname: str
    module: str
    changed_in_function: tuple[int, ...]
    base_source: str
    head_source: str

    @property
    def label(self) -> str:
        return f"{self.path}::{self.qualname}"


@dataclass
class Review:
    base: str
    head: str
    targets: list[Target] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # Every source file the change touched, not only the ones holding a target.
    # The "after" side has to be a state that actually existed: overlaying one
    # file of a multi-file change runs new code against its old helpers, which
    # produces divergences that are artefacts of the overlay and nothing else.
    paths: list[str] = field(default_factory=list)


def resolve(repo: Path, ref: str) -> str:
    out = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref],
                         cwd=repo, capture_output=True, text=True, timeout=60)
    if out.returncode != 0 or not out.stdout.strip():
        raise ValueError(f"unknown revision: {ref}")
    return out.stdout.strip()


def merge_base(repo: Path, base: str, head: str) -> str:
    """Where the branch diverged, not wherever base happens to point now.

    Diffing against the tip of main shows other people's work as part of this
    change; diffing against the merge base shows only what this branch did.
    """
    out = subprocess.run(["git", "merge-base", base, head],
                         cwd=repo, capture_output=True, text=True, timeout=60)
    return out.stdout.strip() or base


def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts and parts[0] in {"src", "lib"}:
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _source_at(repo: Path, ref: str, path: str) -> str | None:
    out = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=repo,
                         capture_output=True, text=True, timeout=120)
    return out.stdout if out.returncode == 0 else None


def plan(repo: Path, base: str, head: str, max_targets: int = 10) -> Review:
    """Which functions this change touched, and which of those we can probe."""
    base_sha, head_sha = resolve(repo, base), resolve(repo, head)
    fork_point = merge_base(repo, base_sha, head_sha)
    return _plan(
        changed_lines(repo, head_sha, fork_point),
        lambda path: _source_at(repo, fork_point, path),
        lambda path: _source_at(repo, head_sha, path),
        fork_point, head_sha, max_targets,
    )


def plan_between(
    before: Path,
    after: Path,
    base: str,
    head: str,
    paths: tuple[str, ...] | None = None,
    max_targets: int = 10,
) -> Review:
    """The same plan, from two directories rather than two git refs.

    This is the path a deployed MUTINY takes: there is no git binary in a
    serverless runtime, and the two revisions arrive as downloaded trees.
    """
    return _plan(
        changed_between(before, after, paths),
        lambda path: _read(before / path),
        lambda path: _read(after / path),
        base, head, max_targets,
    )


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _plan(
    changes: list[ChangedFile],
    read_base,
    read_head,
    base: str,
    head: str,
    max_targets: int,
) -> Review:
    """Shared by both planners: what differs, and which of it can be probed."""
    review = Review(base=base, head=head, paths=[c.path for c in changes])

    for changed in changes:
        head_source = read_head(changed.path)
        base_source = read_base(changed.path)
        if head_source is None:
            review.skipped.append((changed.path, "file is new in this change"))
            continue
        if base_source is None:
            review.skipped.append((changed.path, "no base version to compare against"))
            continue

        names = enclosing_functions(head_source, changed.lines)
        if not names:
            review.skipped.append((changed.path, "changed lines are not inside a function"))
            continue

        for qualname in names[:3]:
            try:
                lo, hi = function_span(head_source, qualname)
                function_span(base_source, qualname)
            except ValueError:
                review.skipped.append(
                    (f"{changed.path}::{qualname}",
                     "ambiguous or absent in one revision"))
                continue
            inside = tuple(n for n in changed.lines if lo <= n <= hi)
            review.targets.append(Target(
                path=changed.path,
                qualname=qualname,
                module=module_name(changed.path),
                changed_in_function=inside or changed.lines,
                base_source=base_source,
                head_source=head_source,
            ))
            if len(review.targets) >= max_targets:
                return review
    return review


def hunk(repo: Path, base: str, head: str, path: str, context: int = 4) -> str:
    out = subprocess.run(
        ["git", "diff", f"--unified={context}", f"{base}..{head}", "--", path],
        cwd=repo, capture_output=True, text=True, timeout=120)
    return out.stdout


def changed_files(repo: Path, base: str, head: str) -> list[ChangedFile]:
    return changed_lines(repo, head, base)
