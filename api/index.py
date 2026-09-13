"""Vercel entry point.

Vercel serves one Python function per file under `api/`, and `vercel.json`
rewrites every path here, so this module is the whole deployment. The ASGI app
is imported rather than defined: the server is the same one `uvicorn
app.server:app` runs locally, and there is no deployment-only code path to
diverge.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.server import app  # noqa: E402,F401 - Vercel looks for `app`
