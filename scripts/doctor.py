#!/usr/bin/env python
"""Check that this machine can actually reach everything MUTINY needs.

Run after cloning, after a network change, or when something starts failing with
a certificate error:  .venv/bin/python scripts/doctor.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mutiny  # noqa: F401  - importing applies the TLS trust fix
from mutiny import tls
from mutiny.config import have_nebius, nebius_base_url, tavily_api_key

OK, BAD = "  ok  ", " FAIL "


def line(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{OK if ok else BAD}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main() -> int:
    print("MUTINY doctor\n" + "-" * 60)
    healthy = True

    bundle = tls.BUNDLE
    healthy &= line("CA bundle", bundle.is_file(), f"{tls.count()} certs at {bundle}")

    reachable, detail = tls.check(nebius_base_url().rstrip("/") + "/models")
    healthy &= line("Token Factory TLS", reachable, detail)

    healthy &= line("NEBIUS_API_KEY", have_nebius(), "set in .env" if have_nebius() else "missing")
    line("TAVILY_API_KEY", tavily_api_key() is not None, "optional")

    if have_nebius():
        from mutiny.models import NemotronClient

        client = NemotronClient()
        try:
            models = {m.id for m in client.client.models.list().data}
            wanted = ["nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B",
                      "nvidia/nemotron-3-super-120b-a12b",
                      "nvidia/Nemotron-3-Ultra-550b-a55b"]
            for m in wanted:
                healthy &= line(f"model {m.split('/')[-1]}", m in models)
        except Exception as exc:  # noqa: BLE001
            healthy &= line("model listing", False, f"{type(exc).__name__}: {exc}")

        print("-" * 60)
        print(client.ledger.summary())

    print("-" * 60)
    print("healthy" if healthy else "problems found — see FAIL lines above")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
