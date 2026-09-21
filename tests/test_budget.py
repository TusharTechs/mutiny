"""What a public deployment is allowed to spend, and whether the cap is real."""
import mutiny.budget as budget


def test_a_run_is_charged_before_it_starts(monkeypatch):
    """Cost is only known at the end, so a counter updated then lets any number
    of simultaneous runs read the same total and all pass the check."""
    monkeypatch.setattr(budget, "_redis", lambda *a: None)
    monkeypatch.setattr(budget, "_spent", 0.0)

    reserved = [budget.reserve() for _ in range(5)]
    assert budget.spent() == 5 * budget.TYPICAL_RUN_USD, (
        "five runs in flight must each be charged, not none of them")

    for r in reserved:
        budget.settle(0.004, r)
    assert round(budget.spent(), 4) == round(5 * 0.004, 4)


def test_settling_cannot_drive_the_total_negative(monkeypatch):
    monkeypatch.setattr(budget, "_redis", lambda *a: None)
    monkeypatch.setattr(budget, "_spent", 0.0)
    budget.settle(0.0, 5.0)
    assert budget.spent() >= 0.0


def test_the_cap_refuses_once_it_is_reached(monkeypatch):
    monkeypatch.setattr(budget, "spent", lambda: budget.TOTAL_USD + 1)
    decision = budget.check("1.2.3.4")
    assert not decision
    assert "spending limit" in decision.reason


def test_one_address_cannot_hold_the_button(monkeypatch):
    monkeypatch.setattr(budget, "spent", lambda: 0.0)
    monkeypatch.setattr(budget, "_seen", {})
    for _ in range(budget.RUNS_PER_HOUR):
        assert budget.check("9.9.9.9")
    assert not budget.check("9.9.9.9")
    assert budget.check("8.8.8.8"), "a different caller is unaffected"


def test_the_command_goes_in_the_body_not_the_path(monkeypatch):
    """The path form needs its arguments encoded, and `mutiny:spent` would then
    depend on the service decoding %3A. Writing to the wrong key would not fail —
    it would keep a second counter nothing reads."""
    sent = {}

    class Response:
        def read(self): return b'{"result": 1.0}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, **kwargs):
        sent["url"] = request.full_url
        sent["body"] = request.data
        return Response()

    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "token")
    monkeypatch.setattr(budget.urllib.request, "urlopen", fake_urlopen)

    budget._redis("incrbyfloat", "mutiny:spent", "0.004")
    assert sent["url"] == "https://example.upstash.io"
    assert b'"mutiny:spent"' in sent["body"]
    assert b"%3A" not in sent["body"]


def test_a_store_that_is_down_does_not_take_the_demo_with_it(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "token")

    def explode(*a, **k):
        raise budget.urllib.error.URLError("unreachable")

    monkeypatch.setattr(budget.urllib.request, "urlopen", explode)
    assert budget._redis("ping") is None
    assert budget.check("1.2.3.4"), "the demo keeps working on the per-run cap"
