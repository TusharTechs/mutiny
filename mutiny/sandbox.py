"""Run observations in Nebius Sandboxes instead of locally.

UNVERIFIED. The API key authenticates against ConTree but carries no Sandboxes
permissions, so nothing here has executed against the real service — see
docs/feedback.md. It is written against the SDK's actual surface and kept behind
the same interface as the local executor, so switching is a one-line change once
access is granted.

Why this shape suits the problem. The differential loop is one expensive setup —
clone, install, import, warm — followed by hundreds of cheap, independent
executions that must not see each other's state. ConTree's model fits exactly:
`image.run()` returns a *new* image rather than mutating the old one, so running
N commands against one warm image is N forks from a single checkpoint, isolated
by construction. Locally we pay the setup once and then serialise; here the
forks are concurrent, and the documented ceiling is 50 at a time (confirmed:
`instance_max_concurrency: 50` from the token's own limits).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import tls
from .config import nebius_api_key, nebius_project_id
from .differential import DRIVER, Observation


@dataclass
class SandboxExecutor:
    """A warm image, forked once per batch of expressions."""

    base_image: str = "python:3.12-slim"
    checkpoint_tag: str | None = None
    concurrency: int = 25  # half the documented ceiling, to leave headroom
    _client: object | None = None
    _warm: object | None = None

    @property
    def client(self):
        if self._client is None:
            from contree_sdk import ContreeSync

            tls.apply()
            # The SDK reads NEBIUS_API_KEY and NEBIUS_PROJECT_ID from the
            # environment; Sandboxes routes by project, unlike inference.
            os.environ.setdefault("NEBIUS_API_KEY", nebius_api_key())
            pid = nebius_project_id()
            if pid:
                os.environ.setdefault("NEBIUS_PROJECT_ID", pid)
            self._client = ContreeSync()
        return self._client

    def _call(self, fn):
        def reset():
            self._client = None
        return tls.with_repair(fn, reset=reset)

    def warm(self, repo: Path, install: str = "pip install -e .") -> object:
        """Build the expensive state once: sources uploaded, package installed.

        The returned image is the checkpoint every later execution forks from.
        """
        image = self.client.images.use(self.base_image)
        image = image.apply_files({"/work": str(repo)})
        image = image.run(install, cwd="/work").wait()
        if self.checkpoint_tag:
            image = image.tag_as(self.checkpoint_tag)
        self._warm = image
        return image

    def observe(self, module: str, expressions: list[str]) -> list[Observation]:
        """Evaluate expressions in a fork of the warm checkpoint.

        Mirrors mutiny.differential.observe so the two are interchangeable.
        """
        if self._warm is None:
            raise RuntimeError("call warm() before observe()")

        image = self._warm.apply_files({
            "/tmp/driver.py": DRIVER.encode(),
            "/tmp/exprs.json": json.dumps(expressions).encode(),
        })
        run = image.run(
            f"python /tmp/driver.py {module} /tmp/out.json /tmp/exprs.json",
            cwd="/work",
        ).wait()
        raw = json.loads(run.read("/tmp/out.json"))
        return [
            Observation(r["input"], r["ok"], r.get("value"), r.get("error"),
                        r.get("opaque", False), r.get("type"), r.get("message"))
            for r in raw
        ]


def available() -> tuple[bool, str]:
    """Can this key actually use Sandboxes? Cheap enough to call before relying on it."""
    try:
        from contree_sdk import ContreeSync

        os.environ.setdefault("NEBIUS_API_KEY", nebius_api_key())
        pid = nebius_project_id()
        if pid:
            os.environ.setdefault("NEBIUS_PROJECT_ID", pid)
        who = tls.with_repair(lambda: ContreeSync().get_token_info())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    missing = [k for k in ("spawn", "import") if not who.permissions.get(k)]
    if missing:
        return False, (f"token lacks Sandboxes permissions ({', '.join(missing)}) — "
                       "request Beta access on the Sandboxes page in the console")
    return True, f"concurrency limit {who.limits.get('instance_max_concurrency')}"
