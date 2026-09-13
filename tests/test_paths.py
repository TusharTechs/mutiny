"""Mutable state must never be pinned to a directory that might be read-only."""
import os
import stat
from pathlib import Path


def test_falls_back_when_the_preferred_directory_cannot_be_written(tmp_path):
    """A read-only parent must yield a usable directory, not an exception.

    Two separate deployments failed on this — the certificate bundle and then
    the completion cache — each surfacing far from its cause.
    """
    from mutiny.paths import writable

    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, stat.S_IREAD | stat.S_IEXEC)
    try:
        got = writable(locked / ".cache")
        assert got != locked / ".cache"
        probe = got / "proof"
        probe.write_text("writable")
        assert probe.read_text() == "writable"
    finally:
        os.chmod(locked, stat.S_IRWXU)


def test_uses_the_preferred_directory_when_it_is_writable(tmp_path):
    from mutiny.paths import writable

    wanted = tmp_path / ".cache"
    assert writable(wanted) == wanted
    assert wanted.is_dir()


def test_the_client_survives_a_read_only_package_directory(tmp_path, monkeypatch):
    """NemotronClient builds its cache in __init__; that must not raise."""
    from mutiny import models, paths

    monkeypatch.setattr(paths, "state", lambda name: tmp_path / name)
    client = models.NemotronClient(cap_usd=1.0)
    assert client.cache_dir.is_dir()
    assert client.spent_here == 0.0
