"""Trust repair must trigger on a real certificate rejection and on nothing else.

A false positive here would rewrite a user's trust configuration because of an
unrelated network error, which is exactly the invasiveness we are avoiding.
"""
import ssl

import pytest

from mutiny import tls


@pytest.fixture(autouse=True)
def _reset():
    tls._REPAIRED = False
    yield
    tls._REPAIRED = False


def _chain(inner: BaseException, depth: int = 3) -> BaseException:
    """Wrap `inner` the way httpx and the OpenAI SDK do."""
    outer: BaseException = inner
    for i in range(depth):
        try:
            raise ConnectionError(f"layer {i}") from outer
        except ConnectionError as exc:
            outer = exc
    return outer


def test_detects_a_direct_verification_error():
    exc = ssl.SSLCertVerificationError("certificate verify failed")
    assert tls.is_verification_error(exc)


def test_detects_it_through_a_wrapped_chain():
    inner = ssl.SSLCertVerificationError("certificate verify failed: self-signed")
    assert tls.is_verification_error(_chain(inner))


def test_detects_it_from_message_alone():
    exc = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
    assert tls.is_verification_error(exc)


def test_detects_an_unreadable_bundle():
    """A corrupt SSL_CERT_FILE fails when the context is built, not at request
    time, and reports [X509] PEM lib rather than a verification message. Same
    broken trust store, same remedy."""
    assert tls.is_verification_error(ssl.SSLError("[X509] PEM lib (_ssl.c:4106)"))


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    ConnectionRefusedError("connection refused"),
    ValueError("bad request"),
    RuntimeError("rate limited"),
])
def test_ignores_unrelated_failures(exc):
    assert not tls.is_verification_error(exc)
    assert tls.repair(exc) is False


def test_survives_a_self_referential_cause_chain():
    a = ValueError("a")
    b = ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert tls.is_verification_error(a) is False


def test_repairs_at_most_once_per_process():
    exc = ssl.SSLCertVerificationError("certificate verify failed")
    first = tls.repair(exc)
    second = tls.repair(exc)
    assert first is True, "should repair the first time"
    assert second is False, "a second failure is a real problem, not a trust gap"


def test_importing_mutiny_has_no_side_effects(monkeypatch):
    """The whole point of option 1: an import must not rewrite trust config."""
    import importlib
    import os

    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        monkeypatch.delenv(var, raising=False)
    import mutiny
    importlib.reload(mutiny)
    assert "SSL_CERT_FILE" not in os.environ


def test_a_probe_that_disagrees_with_itself_is_discarded(tmp_path):
    """ShortUUID.uuid() returns a fresh random value every call, so it differs
    across versions every single time. Re-running the comparison endorses it —
    the divergence reproduces because the probe is random, not because behaviour
    changed. A probe must agree with itself before it can testify."""
    import textwrap

    from mutiny.differential import compare, confirm, observe

    (tmp_path / "s.py").write_text(textwrap.dedent('''
        import os
        def stable(x): return x * 2
        def volatile(): return os.urandom(4).hex()
    '''))
    probes = ["stable(2)", "volatile()"]
    before = observe(tmp_path, "s", probes)
    after = observe(tmp_path, "s", probes, baseline=False)

    # volatile() differs between any two runs; stable() never does
    assert {d.input for d in compare(before, after)} == {"volatile()"}

    confirmed, flaky = confirm(
        compare(before, after),
        run_before=lambda e: observe(tmp_path, "s", e),
        run_after=lambda e: observe(tmp_path, "s", e, baseline=False),
    )
    assert confirmed == [], "a random probe must not be reported as a finding"
    assert [d.input for d in flaky] == ["volatile()"]
