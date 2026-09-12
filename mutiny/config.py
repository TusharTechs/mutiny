"""Credentials and endpoints, loaded from the environment or a local .env."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BASE_URL = "https://api.tokenfactory.us-central1.nebius.com/v1/"
_LOADED = False


def load_env(start: Path | None = None) -> Path | None:
    """Read the nearest .env into os.environ without overriding real env vars."""
    global _LOADED
    if _LOADED:
        return None
    here = (start or Path(__file__).resolve().parent).resolve()
    for candidate in [here, *here.parents]:
        env_file = candidate / ".env"
        if env_file.is_file():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            _LOADED = True
            return env_file
    _LOADED = True
    return None


def nebius_api_key(required: bool = True) -> str | None:
    load_env()
    key = os.environ.get("NEBIUS_API_KEY") or None
    if required and not key:
        raise RuntimeError(
            "NEBIUS_API_KEY is not set. Put it in ~/mutiny/.env (gitignored):\n"
            "    NEBIUS_API_KEY=sk-...\n"
        )
    return key


def nebius_base_url() -> str:
    load_env()
    return os.environ.get("NEBIUS_BASE_URL") or DEFAULT_BASE_URL


def tavily_api_key(required: bool = False) -> str | None:
    load_env()
    key = os.environ.get("TAVILY_API_KEY") or None
    if required and not key:
        raise RuntimeError("TAVILY_API_KEY is not set. Put it in ~/mutiny/.env")
    return key


def ca_bundle() -> str | None:
    """Corporate TLS inspection re-signs every connection, so the
    default trust store fails. certs/ca-bundle.pem merges certifi with the local
    proxy roots; regenerate it with scripts/build-ca-bundle.sh."""
    bundle = Path(__file__).resolve().parent.parent / "certs" / "ca-bundle.pem"
    return str(bundle) if bundle.is_file() else None


def apply_tls_trust() -> None:
    """Point every HTTP library at the merged bundle. Idempotent."""
    bundle = ca_bundle()
    if bundle:
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ[var] = bundle


def have_nebius() -> bool:
    return nebius_api_key(required=False) is not None
