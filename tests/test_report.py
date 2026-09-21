"""What the pull request comment says, and when it says nothing at all."""
from mutiny.report import MARKER, collect, render


def events(described, *, divergences=1, error=""):
    stream = [
        {"type": "fetched", "slug": "owner/repo#1"},
        {"type": "review", "base": "aaaa", "head": "bbbb"},
    ]
    if divergences:
        stream.append({
            "type": "result", "function": "f", "probes": 15, "described": described,
            "summary": "It now raises for a falsy argument.",
            "divergences": [{"input": "f(None)", "before": "0.0",
                             "after": "TypeError"}] * divergences,
        })
    stream.append({"type": "verdict", "functions": 1, "seconds": 40.0,
                   "cost": 0.004})
    if error:
        stream.append({"type": "error", "message": error})
    return stream


def test_an_undescribed_change_is_worth_interrupting_a_reviewer_for():
    report = collect(events(described=False))
    assert report.worth_saying() is True
    body = render(report)
    assert "does not mention" in body
    assert "f(None)" in body and "TypeError" in body
    assert MARKER in body


def test_a_change_that_describes_itself_is_not():
    """Ten of the twelve divergences in the corpus were announced in the title.

    Reporting those is noise wearing the costume of diligence, and a reviewer
    interrupted on every pull request stops reading the bot.
    """
    report = collect(events(described=True))
    assert report.worth_saying() is False
    assert "no undescribed behaviour change" in render(report)


def test_no_divergence_at_all_says_nothing():
    report = collect(events(described=None, divergences=0))
    assert report.worth_saying() is False


def test_comment_on_any_overrides_the_silence():
    assert collect(events(described=True)).worth_saying("any") is True
    assert collect(events(described=False)).worth_saying("never") is False


def test_a_described_change_is_still_visible_but_folded_away():
    stream = events(described=False)
    stream.insert(-1, {"type": "result", "function": "g", "probes": 9,
                       "described": True, "summary": "as advertised",
                       "divergences": [{"input": "g()", "before": "1", "after": "2"}]})
    body = render(collect(stream))
    assert "<details>" in body and "`g`" in body


def test_a_failure_is_reported_as_ours():
    report = collect(events(described=None, divergences=0, error="sandbox unavailable"))
    body = render(report)
    assert "could not check" in body
    assert "a problem with MUTINY, not with the change" in body
