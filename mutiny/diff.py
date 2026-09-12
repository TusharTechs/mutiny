"""Which lines did this change touch?

MUTINY attacks a diff, not a repository. Mature code kills almost every
mutation — the gaps are in what was written this week — so the unit of work is
the set of source lines a commit or pull request actually modified.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

# Directory names that mean "this is the suite, not the code under test".
TEST_DIRS = {"test", "tests", "testing", "_test", "_tests"}


def _is_source(path: str) -> bool:
    """Is this a source file we may attack?

    Test files are excluded: mutating the suite to see whether the suite still
    passes is circular, and it produces nonsense survivors -- transposing the
    arguments of an assertEqual "survives" because assertEqual is symmetric.

    Matching is on path segments rather than substrings. A substring check for
    "/tests/" silently misses a top-level `tests/` directory, which has no
    leading slash.
    """
    if not path.endswith(".py"):
        return False
    parts = Path(path).parts
    if any(part.lower() in TEST_DIRS for part in parts[:-1]):
        return False
    name = parts[-1].lower()
    return not (
        name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
    )


@dataclass(frozen=True)
class ChangedFile:
    path: str
    lines: tuple[int, ...]

    def __len__(self) -> int:
        return len(self.lines)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, timeout=120
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()[:200]}")
    return out.stdout


def changed_lines(repo: Path, ref: str = "HEAD", base: str | None = None) -> list[ChangedFile]:
    """Source lines added or modified by `ref` relative to `base` (default: its parent).

    Deletions are excluded: a line that no longer exists cannot be mutated.
    Test files are excluded too — attacking the tests would be circular.
    """
    base = base or f"{ref}^"
    raw = _git(repo, "diff", "--unified=0", "--no-color", f"{base}..{ref}")

    files: dict[str, list[int]] = {}
    current: str | None = None
    for line in raw.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            current = path if _is_source(path) else None
        elif current and (m := HUNK.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count:  # count == 0 means a pure deletion
                files.setdefault(current, []).extend(range(start, start + count))

    return [ChangedFile(p, tuple(sorted(set(ls)))) for p, ls in sorted(files.items()) if ls]


def source_commits(repo: Path, limit: int = 20, since: str | None = None) -> list[str]:
    """Recent commits that touched source files — merge commits skipped."""
    args = ["log", "--no-merges", f"-{limit}", "--format=%H"]
    if since:
        args.append(f"--since={since}")
    args += ["--", "*.py"]
    shas = [s for s in _git(repo, *args).splitlines() if s.strip()]
    return [s for s in shas if changed_lines(repo, s)]


def enclosing_functions(source: str, lines: tuple[int, ...]) -> list[str]:
    """Qualified names of the functions those lines fall inside.

    A mutation needs its function for context, and the function is also what the
    proof test has to exercise.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[tuple[int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lo, hi = child.lineno, (child.end_lineno or child.lineno)
                if any(lo <= n <= hi for n in lines):
                    found.append((lo, f"{prefix}{child.name}"))
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    # Innermost wins: a nested helper is more specific than its enclosing method.
    return [name for _, name in sorted(found, key=lambda t: -t[0])]
