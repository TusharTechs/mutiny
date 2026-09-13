"""Which lines did this change touch?

MUTINY attacks a diff, not a repository. Mature code kills almost every
mutation — the gaps are in what was written this week — so the unit of work is
the set of source lines a commit or pull request actually modified.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
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


def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None


def changed_between(
    before: Path, after: Path, paths: Iterable[str] | None = None
) -> list[ChangedFile]:
    """The same question as changed_lines, asked of two directories.

    `git diff` is not available everywhere MUTINY needs to run -- a serverless
    runtime ships a Python and little else -- and the comparison does not
    actually need git. Two trees on disk and difflib answer it identically.

    Deletions are excluded, as before: a line that no longer exists cannot be
    probed. Line numbers refer to the `after` tree, which is the version that
    will be executed.
    """
    import difflib

    if paths is None:
        candidates = sorted(str(p.relative_to(after))
                            for p in after.rglob("*.py") if p.is_file())
    else:
        candidates = sorted(set(paths))

    changed: list[ChangedFile] = []
    for path in candidates:
        if not _is_source(path):
            continue
        new = _lines(after / path)
        if new is None:
            continue
        old = _lines(before / path)
        if old is None:
            # New file: every line is new. review.plan reports it as skipped,
            # but it has to reach that decision rather than vanish here.
            changed.append(ChangedFile(path, tuple(range(1, len(new) + 1))))
            continue
        if old == new:
            continue

        touched: list[int] = []
        for tag, _, _, j1, j2 in difflib.SequenceMatcher(
            a=old, b=new, autojunk=False
        ).get_opcodes():
            if tag in ("replace", "insert"):
                touched.extend(range(j1 + 1, j2 + 1))
        if touched:
            changed.append(ChangedFile(path, tuple(sorted(set(touched)))))
    return changed


def hunk_between(before: Path, after: Path, path: str, context: int = 4) -> str:
    """A unified diff of one file between two trees, for showing to a reader."""
    import difflib

    old = _lines(before / path) or []
    new = _lines(after / path) or []
    return "\n".join(difflib.unified_diff(
        old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="", n=context))


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
