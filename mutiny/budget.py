"""How much this deployment is allowed to spend, and what it has spent.

A public URL that calls a paid model is a way to lose money to strangers. The
per-run cap in `NemotronClient` bounds one request; it says nothing about ten
thousand of them.

The difficulty is that a serverless deployment has no memory between requests --
each one may be a fresh process -- so a counter in a variable protects nothing.
When `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are set the total is
kept in Redis over its REST API (no client library, no new dependency) and the
cap is real. Without them the counter is per-process and the honest description
is that only the per-run cap applies.

Nothing here is a security boundary on its own. It is a spend ceiling.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

TOTAL_USD = float(os.environ.get("MUTINY_BUDGET_USD", "15"))
PER_RUN_USD = float(os.environ.get("MUTINY_RUN_CAP_USD", "0.25"))

# One visitor should not be able to occupy the whole budget by holding the
# button down. Runs take half a minute, so this is generous.
RUNS_PER_HOUR = int(os.environ.get("MUTINY_RUNS_PER_HOUR", "20"))

# Charged before a run starts and corrected when it finishes. A measured run is
# about half a cent; this is deliberately above that.
TYPICAL_RUN_USD = float(os.environ.get("MUTINY_TYPICAL_RUN_USD", "0.02"))

TIMEOUT = 5
_local = threading.Lock()
_spent = 0.0
_seen: dict[str, list[float]] = {}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    spent: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


def _redis(*command: str) -> float | None:
    """Run one Redis command over Upstash's REST API.

    The command goes in the body as a JSON array rather than in the URL path.
    Both forms exist; the path form needs its arguments URL-encoded, and a key
    like `mutiny:spent` then depends on the service decoding `%3A` back to a
    colon. Writing to the wrong key would not fail — it would silently keep a
    second counter that nothing ever reads, which is the failure this whole
    module exists to prevent.
    """
    url, token = (os.environ.get("UPSTASH_REDIS_REST_URL"),
                  os.environ.get("UPSTASH_REDIS_REST_TOKEN"))
    if not url or not token:
        return None
    request = urllib.request.Request(
        url.rstrip("/"), data=json.dumps(list(command)).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        context = ssl.create_default_context(
            cafile=os.environ.get("SSL_CERT_FILE") or None)
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            body = json.loads(response.read())
        return float(body.get("result") or 0)
    except (urllib.error.URLError, ValueError, TypeError):
        # A budget store that is down must not take the demo down with it. The
        # per-run cap still applies.
        return None


def shared() -> bool:
    """Is the ceiling actually enforced, or only the per-run cap?

    Worth being able to answer from outside. Without a shared store the counter
    resets whenever a new serverless instance starts, and a cap that quietly
    does nothing is worse than no cap, because it is believed.
    """
    return _redis("ping") is not None or bool(
        os.environ.get("UPSTASH_REDIS_REST_URL")
        and os.environ.get("UPSTASH_REDIS_REST_TOKEN"))


def spent() -> float:
    shared = _redis("get", "mutiny:spent")
    if shared is not None:
        return shared
    with _local:
        return _spent


def record(usd: float) -> None:
    """Add the cost of a finished run to the running total."""
    global _spent
    if usd <= 0:
        return
    if _redis("incrbyfloat", "mutiny:spent", f"{usd:.6f}") is None:
        with _local:
            _spent += usd


def reserve() -> float:
    """Charge an estimate up front, before the run starts.

    Cost is only known when a run finishes, so a counter updated at the end
    lets any number of simultaneous runs read the same total and all pass the
    check. Someone holding the button, or a script, spends the ceiling many
    times over before it notices. Charging first and correcting afterwards
    bounds that to the number of runs actually in flight.
    """
    record(TYPICAL_RUN_USD)
    return TYPICAL_RUN_USD


def settle(actual: float, reserved: float) -> None:
    """Correct the estimate once the real cost is known."""
    difference = round(actual - reserved, 6)
    if abs(difference) < 1e-6:
        return
    if _redis("incrbyfloat", "mutiny:spent", f"{difference:.6f}") is None:
        global _spent
        with _local:
            _spent = max(0.0, _spent + difference)


def check(client_id: str) -> Decision:
    """May this caller start a run now?"""
    total = spent()
    if total >= TOTAL_USD:
        return Decision(False, (
            "This demo has reached its spending limit. The code is on GitHub and "
            "runs locally with your own Nebius key."), total)

    now = time.monotonic()
    with _local:
        recent = [t for t in _seen.get(client_id, []) if now - t < 3600]
        if len(recent) >= RUNS_PER_HOUR:
            return Decision(False, (
                f"That is {RUNS_PER_HOUR} runs in an hour from one address. "
                "Try again later, or run it locally."), total)
        recent.append(now)
        _seen[client_id] = recent
        if len(_seen) > 4096:  # do not grow without bound
            _seen.clear()
    return Decision(True, "", total)
