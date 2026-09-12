"""MUTINY — adversarial verification for AI-written code."""
from . import tls as _tls

# Corporate TLS inspection breaks every outbound call from Python, Node and uv.
# Fixing it at import time means no module, script or subprocess has to remember.
_tls.apply()

__version__ = "0.1.0"
