"""A single attack, and the machinery to apply and revert it."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


class MutationError(RuntimeError):
    """The mutation could not be applied unambiguously."""


@dataclass(frozen=True)
class Mutation:
    """An attack on one exact span of source text.

    The span is located by (line, original) rather than a byte offset so that a
    mutation stays meaningful if the file is reformatted, and so that an
    ambiguous match is an error rather than a silent multi-site edit.
    """

    path: str  # repo-relative
    line: int  # 1-indexed
    original: str  # exact text to replace, within that line
    mutated: str  # replacement text
    bug_class: str
    rationale: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        if self.original == self.mutated:
            raise MutationError(f"{self.id or self.path}: mutation is a no-op")

    def _patched_source(self, root: Path) -> str:
        target = root / self.path
        try:
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        except FileNotFoundError as exc:
            raise MutationError(f"no such file: {target}") from exc

        idx = self.line - 1
        if not (0 <= idx < len(lines)):
            raise MutationError(
                f"{self.path}:{self.line} out of range (file has {len(lines)} lines)"
            )

        line = lines[idx]
        occurrences = line.count(self.original)
        if occurrences == 0:
            raise MutationError(
                f"{self.path}:{self.line} does not contain {self.original!r}\n"
                f"  line is: {line.rstrip()!r}"
            )
        if occurrences > 1:
            raise MutationError(
                f"{self.path}:{self.line} contains {self.original!r} "
                f"{occurrences} times — span is ambiguous"
            )

        lines[idx] = line.replace(self.original, self.mutated)
        return "".join(lines)

    @contextlib.contextmanager
    def applied(self, root: Path) -> Iterator[Path]:
        """Patch the file in place for the duration of the block, then restore."""
        target = root / self.path
        backup = target.read_text(encoding="utf-8")
        patched = self._patched_source(root)
        target.write_text(patched, encoding="utf-8")
        try:
            yield root
        finally:
            target.write_text(backup, encoding="utf-8")

    @property
    def label(self) -> str:
        return self.id or f"{self.path}:{self.line} {self.original!r}->{self.mutated!r}"
