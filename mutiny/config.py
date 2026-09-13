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


def nebius_project_id(required: bool = False) -> str | None:
    """Sandboxes routes by project as well as by token; inference does not."""
    load_env()
    pid = os.environ.get("NEBIUS_PROJECT_ID") or None
    if required and not pid:
        raise RuntimeError(
            "NEBIUS_PROJECT_ID is not set. Find it on the Sandboxes page in the "
            "Token Factory console and add it to .env:\n"
            "    NEBIUS_PROJECT_ID=aiproject-...\n"
        )
    return pid


def tavily_api_key(required: bool = False) -> str | None:
    load_env()
    key = os.environ.get("TAVILY_API_KEY") or None
    if required and not key:
        raise RuntimeError("TAVILY_API_KEY is not set. Put it in ~/mutiny/.env")
    return key


def ca_bundle() -> str | None:
    """Path to the merged CA bundle. See mutiny.tls for why this exists."""
    from .tls import BUNDLE

    return str(BUNDLE) if BUNDLE.is_file() else None


def apply_tls_trust() -> None:
    """Install the merged CA bundle eagerly.

    Only needed by entry points that want trust configured before the first
    request — doctor.py, for instance. Normal code paths repair reactively
    instead; see mutiny.tls.repair.
    """
    from .tls import apply

    apply()


def have_nebius() -> bool:
    return nebius_api_key(required=False) is not None


def github_token() -> str | None:
    """Optional. Only raises GitHub's rate limit; no scopes are needed."""
    load_env()
    return os.environ.get("GITHUB_TOKEN") or None
