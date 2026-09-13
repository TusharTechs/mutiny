"""Session-level orchestration: target selection and its fallbacks."""
from pathlib import Path
from types import SimpleNamespace



def test_repository_walks_to_the_next_candidate_when_probes_fail(monkeypatch):
    """A target that yields no probe must hand over, not end the run.

    The first version of this checked the wrong key on the event dict, so the
    fallback silently never fired and django reported nothing.
    """
    from mutiny import session

    monkeypatch.setattr(session, "_candidate_functions", lambda *a, **k: ["first", "second"])
    monkeypatch.setattr(session, "available", lambda: (False, "test"))

    attempted = []

    def fake_verify(repo, qualname, **kwargs):
        attempted.append(qualname)
        if qualname == "first":
            yield {"type": "no_probes", "function": qualname}
        else:
            yield {"type": "result", "function": qualname, "divergences": []}

    monkeypatch.setattr(session, "verify_function", fake_verify)

    source = SimpleNamespace(path=Path("."), slug="owner/repo")
    events = list(session._verify_repository(source, probes=4, forks=2, cap=1.0))

    assert attempted == ["first", "second"]
    assert not any(e["type"] == "no_probes" for e in events)
    assert [e for e in events if e["type"] == "result"][0]["function"] == "second"
