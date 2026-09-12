"""Make TLS work behind a corporate inspecting proxy, once, for everything.

A TLS-inspecting middlebox terminates the
connection and re-signs it with a CA that only the machine's own trust store
knows about. curl works because macOS keychain has that CA; Python, Node, uv and
requests do not consult the keychain, so they fail with
``CERTIFICATE_VERIFY_FAILED`` on every outbound call.

The fix is one merged bundle — certifi's public roots plus whatever the admin
installed locally — exported to a file and pointed at via the environment
variables each toolchain reads. ``apply()`` runs on ``import mutiny``, builds the
bundle if it is missing or stale, and is safe to call repeatedly.

Deliberately *not* hardcoding the proxy's CA names: they differ per vendor and
get rotated. Everything in the system keychain is admin-installed by definition,
so taking all of it is both more general and more robust.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent / "certs" / "ca-bundle.pem"
MAX_AGE_DAYS = 7

# Every toolchain we touch reads a different variable for the same thing.
TRUST_VARS = (
    "SSL_CERT_FILE",       # python ssl, openssl
    "REQUESTS_CA_BUNDLE",  # requests
    "CURL_CA_BUNDLE",      # curl, some SDKs
    "NODE_EXTRA_CA_CERTS", # node / next.js
    "UV_NATIVE_TLS",       # see below - handled specially
)
_MACOS_KEYCHAINS = (
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
)


def _system_certs() -> str:
    """PEM for every certificate the machine's admin installed."""
    if platform.system() != "Darwin" or not shutil.which("security"):
        return ""
    chunks = []
    for keychain in _MACOS_KEYCHAINS:
        if not Path(keychain).exists():
            continue
        try:
            out = subprocess.run(
                ["security", "find-certificate", "-a", "-p", keychain],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if out.returncode == 0 and "BEGIN CERTIFICATE" in out.stdout:
                chunks.append(out.stdout)
        except (subprocess.SubprocessError, OSError):
            continue
    return "\n".join(chunks)


def _stale() -> bool:
    if not BUNDLE.is_file() or BUNDLE.stat().st_size == 0:
        return True
    return (time.time() - BUNDLE.stat().st_mtime) > MAX_AGE_DAYS * 86400


def build(force: bool = False) -> Path | None:
    """Write certifi's roots plus local CAs to certs/ca-bundle.pem."""
    if not force and not _stale():
        return BUNDLE
    try:
        import certifi
    except ImportError:
        return BUNDLE if BUNDLE.is_file() else None

    parts = [Path(certifi.where()).read_text(encoding="utf-8")]

    local = _system_certs()
    if local:
        parts.append(local)

    # Whatever the environment already pointed at, if it is a real file and not
    # our own bundle (which would recurse).
    existing = os.environ.get("SSL_CERT_FILE")
    if existing and Path(existing).is_file() and Path(existing) != BUNDLE:
        parts.append(Path(existing).read_text(encoding="utf-8", errors="ignore"))

    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE.write_text("\n".join(parts), encoding="utf-8")
    return BUNDLE


def count() -> int:
    if not BUNDLE.is_file():
        return 0
    return BUNDLE.read_text(encoding="utf-8", errors="ignore").count("BEGIN CERTIFICATE")


def apply(force_rebuild: bool = False) -> Path | None:
    """Build the bundle if needed and point every toolchain at it.

    Sets the environment in-process, so anything we spawn inherits it — the
    pytest subprocesses in runner.py included.
    """
    bundle = build(force=force_rebuild)
    if bundle is None or not bundle.is_file():
        return None
    path = str(bundle)
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"):
        os.environ[var] = path
    # uv verifies against the platform store when told to; without this it uses
    # its own bundled roots and fails behind the proxy.
    os.environ.setdefault("UV_NATIVE_TLS", "1")
    return bundle


def check(url: str = "https://api.tokenfactory.us-central1.nebius.com/v1/models") -> tuple[bool, str]:
    """Prove the bundle works against a real endpoint. Used by scripts/doctor.py."""
    import ssl
    import urllib.error
    import urllib.request

    apply()
    ctx = ssl.create_default_context(cafile=str(BUNDLE))
    try:
        urllib.request.urlopen(urllib.request.Request(url), timeout=30, context=ctx)
        return True, "reachable"
    except urllib.error.HTTPError as exc:
        # 401/403 means TLS succeeded and only auth was missing — that is a pass.
        return True, f"TLS ok (HTTP {exc.code})"
    except Exception as exc:  # noqa: BLE001 - report whatever went wrong
        return False, f"{type(exc).__name__}: {exc}"
