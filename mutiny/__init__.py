"""MUTINY — adversarial verification for AI-written code."""

__version__ = "0.1.0"

# Importing this package deliberately has no side effects. TLS trust is repaired
# reactively when a request is actually rejected (see mutiny.tls.repair), so a
# machine that does not sit behind an inspecting proxy is left alone.
