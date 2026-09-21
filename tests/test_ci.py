"""Finding the pull request, and speaking at most once in it."""
import json

import pytest

from mutiny import ci


def test_reads_the_pull_request_from_the_event_payload(tmp_path):
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"pull_request": {"number": 42}}))
    context = ci.context({"GITHUB_REPOSITORY": "owner/repo",
                          "GITHUB_EVENT_PATH": str(payload),
                          "GITHUB_TOKEN": "t"})
    assert (context.owner, context.repo, context.pull_request) == ("owner", "repo", 42)
    assert context.url == "https://github.com/owner/repo/pull/42"


def test_an_explicit_number_wins(tmp_path):
    context = ci.context({"GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "7"})
    assert context.pull_request == 7


def test_says_what_to_do_when_run_outside_an_action():
    with pytest.raises(ci.CIError, match="pass --url"):
        ci.context({})
    with pytest.raises(ci.CIError, match="PR_NUMBER"):
        ci.context({"GITHUB_REPOSITORY": "owner/repo"})


def test_a_quiet_run_with_nothing_said_before_stays_quiet(monkeypatch):
    monkeypatch.setattr(ci, "existing_comment", lambda ctx: None)
    monkeypatch.setattr(ci, "_request", lambda *a, **k: pytest.fail("posted anyway"))
    context = ci.Context("o", "r", 1, "t")
    assert "staying quiet" in ci.publish(context, "body", speak=False)


def test_a_warning_that_is_fixed_is_corrected_not_left_standing(monkeypatch):
    """Pushing a fix must replace the warning, not leave it above a correction."""
    calls = []
    monkeypatch.setattr(ci, "existing_comment", lambda ctx: 99)
    monkeypatch.setattr(ci, "_request",
                        lambda url, token, method="GET", body=None:
                        calls.append((method, url, body)))
    context = ci.Context("o", "r", 1, "t")
    assert "updated" in ci.publish(context, "all clear now", speak=False)
    assert calls[0][0] == "PATCH"
    assert "comments/99" in calls[0][1]
    assert calls[0][2]["body"] == "all clear now"


def test_a_first_finding_is_posted(monkeypatch):
    calls = []
    monkeypatch.setattr(ci, "existing_comment", lambda ctx: None)
    monkeypatch.setattr(ci, "_request",
                        lambda url, token, method="GET", body=None:
                        calls.append((method, url, body)))
    assert ci.publish(ci.Context("o", "r", 1, "t"), "found it", speak=True) == "commented"
    assert calls[0][0] == "POST" and calls[0][2]["body"] == "found it"


def test_a_run_that_could_not_happen_is_not_a_quiet_success(capsys):
    """The first live run reported 'staying quiet' with empty credentials.

    A green job and no comment is what a clean pull request looks like. A tool
    that reports a configuration failure the same way is worse than no tool.
    """
    ci.annotate("NEBIUS_API_KEY is not set")
    printed = capsys.readouterr().out
    assert printed.startswith("::error title=MUTINY::")
    assert "NEBIUS_API_KEY is not set" in printed
