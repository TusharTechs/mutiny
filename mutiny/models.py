"""Nemotron access with a hard spend cap, a token ledger, and on-disk caching.

Credits are the scarce resource in this project, so three properties matter more
than throughput:

* every response is cached by content hash, so re-running an experiment is free;
* every call is recorded with real token counts from the API;
* the cap is checked before each request and raises rather than overspending.

Nemotron 3 always reasons. The trace arrives in a separate ``reasoning`` field but
is billed as completion tokens, and there is no way to switch it off on Token
Factory today -- ``chat_template_kwargs={"thinking": False}`` is silently ignored
and ``/no_think`` makes the trace longer. Budget generously for ``max_tokens``:
too small an allowance is spent entirely on reasoning and returns empty content
with ``finish_reason == "length"``.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import tls
from .config import nebius_api_key, nebius_base_url

NANO = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
SUPER = "nvidia/nemotron-3-super-120b-a12b"
ULTRA = "nvidia/Nemotron-3-Ultra-550b-a55b"
LIGHTNING = "nvidia/Nemotron-3_5-Lightning"

# USD per 1M tokens. ESTIMATES — token counts below come from the API and are
# exact; these rates are not. Correct them once billing confirms real numbers.
PRICES: dict[str, tuple[float, float]] = {
    NANO: (0.05, 0.20),
    SUPER: (0.25, 0.75),
    ULTRA: (1.00, 3.00),
    LIGHTNING: (0.10, 0.40),
}
_DEFAULT_PRICE = (0.50, 1.50)


class BudgetExceeded(RuntimeError):
    """The spend cap would be breached by this call."""


@dataclass
class Call:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    seconds: float
    cached: bool
    tag: str = ""
    finish_reason: str = "stop"
    reasoning_chars: int = 0

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


@dataclass
class Ledger:
    path: Path
    calls: list[Call] = field(default_factory=list)

    def load(self) -> "Ledger":
        if self.path.is_file():
            raw = json.loads(self.path.read_text())
            self.calls = [Call(**c) for c in raw.get("calls", [])]
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"total_usd": self.total_usd, "calls": [asdict(c) for c in self.calls]}, indent=2)
        )

    def add(self, call: Call) -> None:
        self.calls.append(call)
        self.save()

    @property
    def total_usd(self) -> float:
        return round(sum(c.cost_usd for c in self.calls), 6)

    @property
    def billed_calls(self) -> int:
        return sum(1 for c in self.calls if not c.cached)

    def summary(self) -> str:
        by_model: dict[str, list[Call]] = {}
        for c in self.calls:
            by_model.setdefault(c.model, []).append(c)
        lines = [f"spend ${self.total_usd:.4f} over {self.billed_calls} billed calls "
                 f"({len(self.calls) - self.billed_calls} served from cache)"]
        for model, calls in sorted(by_model.items()):
            billed = [c for c in calls if not c.cached]
            tin = sum(c.prompt_tokens for c in billed)
            tout = sum(c.completion_tokens for c in billed)
            spend = sum(c.cost_usd for c in billed)
            lines.append(
                f"  {model.split('/')[-1]:36s} {len(billed):3d} calls  "
                f"{tin:7d} in  {tout:6d} out  ${spend:.4f}"
            )
        return "\n".join(lines)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES.get(model, _DEFAULT_PRICE)
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


class NemotronClient:
    def __init__(
        self,
        cap_usd: float = 2.00,
        cache_dir: Path | None = None,
        ledger_path: Path | None = None,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.cache_dir = cache_dir or root / ".cache" / "completions"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(ledger_path or root / ".cache" / "ledger.json").load()
        self.cap_usd = cap_usd
        self._client: Any = None

    # -- lazily construct so importing this module never needs a key
    @property
    def client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=nebius_api_key(), base_url=nebius_base_url())
        return self._client

    def _with_tls_repair(self, call: Any) -> Any:
        """Run `call`; if the certificate chain is rejected, fix trust and retry.

        The client is discarded before retrying because httpx resolves its SSL
        context when it is constructed — repairing the environment does nothing
        for a connection pool that was already built.
        """
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised unless we can repair
            if not tls.repair(exc):
                raise
            self._client = None
            return call()

    def _key(self, payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:32]

    def _complete_once(
        self,
        messages: list[dict[str, str]],
        model: str = SUPER,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        tag: str = "",
        allow_network: bool = True,
    ) -> tuple[str, Call]:
        payload = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "response_format": response_format,
        }
        key = self._key(payload)
        cached_at = self.cache_dir / f"{key}.json"

        if cached_at.is_file():
            rec = json.loads(cached_at.read_text())
            call = Call(model, rec["prompt_tokens"], rec["completion_tokens"], 0.0, 0.0,
                        True, tag, rec.get("finish_reason", "stop"),
                        rec.get("reasoning_chars", 0))
            self.ledger.add(call)
            return rec["text"], call

        if not allow_network:
            raise BudgetExceeded(f"cache miss for {tag or key} and network disabled")

        # Cap check uses a conservative upper bound: we cannot know the prompt
        # token count before the call, so assume the request is as large as the
        # model's reply allowance plus a generous prompt.
        worst_case = estimate_cost(model, 40_000, max_tokens)
        if self.ledger.total_usd + worst_case > self.cap_usd:
            raise BudgetExceeded(
                f"cap ${self.cap_usd:.2f} would be exceeded: spent "
                f"${self.ledger.total_usd:.4f}, worst case for this call "
                f"${worst_case:.4f}. Raise cap_usd deliberately if you mean to."
            )

        kwargs: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        }
        if response_format:
            kwargs["response_format"] = response_format

        started = time.monotonic()
        resp = self._with_tls_repair(lambda: self.client.chat.completions.create(**kwargs))
        elapsed = time.monotonic() - started

        choice = resp.choices[0]
        text = choice.message.content or ""
        reasoning = getattr(choice.message, "reasoning", None) or ""
        finish_reason = choice.finish_reason or "stop"
        usage = resp.usage
        pt = getattr(usage, "prompt_tokens", 0) or 0
        ct = getattr(usage, "completion_tokens", 0) or 0
        cost = estimate_cost(model, pt, ct)

        cached_at.write_text(json.dumps({
            "text": text, "prompt_tokens": pt, "completion_tokens": ct,
            "model": model, "finish_reason": finish_reason,
            "reasoning_chars": len(reasoning),
        }))
        call = Call(model, pt, ct, cost, round(elapsed, 2), False, tag,
                    finish_reason, len(reasoning))
        self.ledger.add(call)
        return text, call

    def complete(
        self,
        messages: list[dict[str, Any]],
        model: str = SUPER,
        max_tokens: int = 1200,
        temperature: float = 0.2,
        response_format: dict[str, Any] | None = None,
        tag: str = "",
        allow_network: bool = True,
        auto_widen: int = 3,
    ) -> tuple[str, Call]:
        """Complete, widening the allowance if reasoning consumed all of it.

        Nemotron reasons before answering and the trace bills against max_tokens,
        so an allowance that looks generous can be spent entirely on thinking --
        returning empty content, finish_reason "length", and no error at all.
        Observed in practice: Nano emitted 23,000 characters of reasoning and no
        answer, four times out of seven. Retrying with a wider allowance is the
        only remedy, and doing it here means no caller can forget it.
        """
        budget = max_tokens
        text, call = "", None
        for widening in range(max(1, auto_widen)):
            text, call = self._complete_once(
                messages, model=model, max_tokens=budget, temperature=temperature,
                response_format=response_format,
                tag=f"{tag}@{budget}" if widening else tag,
                allow_network=allow_network,
            )
            if not call.truncated or text.strip():
                return text, call
            budget *= 2
        assert call is not None
        return text, call
