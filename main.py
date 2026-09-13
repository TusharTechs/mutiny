"""Entry point for a deployment.

Vercel looks for a top-level `app` in one of a handful of filenames and routes
every request to it. This is that file, and it is deliberately thin: the ASGI
application is the same object `uvicorn app.server:app` serves locally, so there
is no deployment-only code path that can drift from the one that gets tested.

The path insertion is what makes `mutiny` and `app` importable when this module
is loaded from the project root rather than installed as a package.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.server import app  # noqa: E402,F401 - Vercel loads this name
