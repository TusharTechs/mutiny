"""Where mutable state can actually be written.

The obvious home for a cache, a ledger or a rebuilt certificate bundle is beside
the code. On a laptop that is right. On a deployment the code sits in a
read-only filesystem, and the failure does not look like a permissions problem:
it surfaces three layers up as "sandboxes unavailable", or as an OSError in the
middle of reviewing a pull request. That happened twice on the same deployment,
for two different directories, which is what this module is for.

Preferred location if it is writable, temporary directory if it is not. Nothing
kept here is precious — a cold cache costs time, not correctness.
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def writable(preferred: Path) -> Path:
    """`preferred` if it can be written to, otherwise somewhere that can be.

    The check is a real write. Probing permission bits gets the answer wrong on
    read-only mounts, which is precisely the case this exists to handle.
    """
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".writable"
        probe.write_text("")
        probe.unlink()
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "mutiny" / preferred.name
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


def state(name: str) -> Path:
    """A writable directory for `name`, next to the package when possible."""
    return writable(Path(__file__).resolve().parent.parent / name)
