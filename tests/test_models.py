import json


def test_cap_is_per_client_not_lifetime(tmp_path, monkeypatch):
    """cap_usd must limit this run, not everything ever spent.

    The ledger is cumulative and persists between runs. Checking the cap against
    its total made a fresh client with a $5 cap refuse to work the moment
    lifetime spend passed $5 — which it silently did.
    """
    from mutiny.models import SUPER, BudgetExceeded, NemotronClient

    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"calls": [{
        "model": "nvidia/nemotron-3-super-120b-a12b", "prompt_tokens": 1000,
        "completion_tokens": 1000, "cost_usd": 9.99, "seconds": 1.0,
        "cached": False, "tag": "earlier run"}]}))

    client = NemotronClient(cap_usd=1.0, cache_dir=tmp_path / "c", ledger_path=ledger)
    assert client.ledger.total_usd >= 9.0
    assert client.spent_here == 0.0

    # A call that fits inside this run's cap must not be refused because of
    # what earlier runs cost.
    try:
        client._complete_once(
            [{"role": "user", "content": "x"}], model=SUPER, max_tokens=100,
            temperature=0.0, tag="t", allow_network=False)
    except BudgetExceeded as exc:
        assert "cache miss" in str(exc), f"refused for the wrong reason: {exc}"
