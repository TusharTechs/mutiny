

def _answer(monkeypatch, body: str, reply: str):
    from mutiny import explain as explain_mod

    captured = {}

    class FakeClient:
        def complete(self, messages, **kwargs):
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return reply, type("Call", (), {"cost_usd": 0.0})()

    return explain_mod._describes(
        FakeClient(), "f", body,
        [{"input": "f(None)", "before": "0.0", "after": "TypeError"}])


def test_a_refactor_that_changed_behaviour_is_not_described(monkeypatch):
    """The case the tool exists for must not be labelled 'as described'.

    A live run flipped this to YES because the pull request body discussed the
    very code that changed. Discussing the code is not predicting the
    difference: the body argued the old branch was unreachable, and the
    measurement shows it was reached.
    """
    assert _answer(monkeypatch, "refactor: drop always-true truthiness checks", "NO") is False


def test_a_fix_that_says_it_changes_a_result_is_described(monkeypatch):
    assert _answer(monkeypatch, "fix: bump_build returned an unchanged version", "YES") is True


def test_no_description_is_not_an_accusation(monkeypatch):
    assert _answer(monkeypatch, "", "NO") is None
    assert _answer(monkeypatch, "something", "UNCLEAR") is None


def test_the_rule_about_preserving_changes_reaches_the_model(monkeypatch):
    from mutiny import explain as explain_mod

    rule = " ".join(explain_mod.INTENT_SYSTEM.split())
    assert "behaviour-preserving" in rule
    assert "Discussing the code is not the same as predicting the difference" in rule
