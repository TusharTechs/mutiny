"""Run observations in Nebius Sandboxes, forked from one warm checkpoint.

The differential loop is one expensive setup — upload the repository, install it,
import it — followed by hundreds of short independent executions that must not
see each other's state. ConTree's model is a direct fit: a run with
``disposable=False`` yields a persistent image, and every later run against that
image starts from it without altering it. N probes against one warm image are N
forks from a single checkpoint, isolated by construction.

Verified against the live service: a fork inherits everything the checkpoint
wrote, sees nothing any sibling wrote, and leaves the checkpoint untouched.

Results come back on stdout rather than through a file, because the forks
themselves are disposable and there is no image left to read from afterwards.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import tls
from .config import nebius_api_key, nebius_project_id
from .differential import DRIVER, Observation

MARKER = "\x00MUTINY\x00"


SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
             ".ruff_cache", "node_modules", ".tox", "build", "dist", ".eggs"}

EXTRACT = (
    "import tarfile; "
    "tarfile.open('/tmp/repo.tgz').extractall('{dest}', filter='data'); "
    "print('extracted')"
)


def _check(run, what: str):
    """Raise unless the step actually succeeded.

    `shell=` mode returns empty stdout and stderr regardless of outcome, so a
    failing step there looks exactly like a silent success — our `pip install`
    failed this way and nothing surfaced until an import error much later. Every
    step now uses command+args and is checked.
    """
    code = getattr(run, "exit_code", 0)
    if code != 0:
        raise RuntimeError(
            f"{what} failed (exit {code}): {(run.stderr or run.stdout or '').strip()[-400:]}"
        )
    return run


def _client():
    from contree_sdk import ContreeSync
    from contree_sdk.config import ContreeConfig

    tls.apply()
    os.environ.setdefault("NEBIUS_API_KEY", nebius_api_key())
    pid = nebius_project_id()
    if pid:
        os.environ.setdefault("NEBIUS_PROJECT_ID", pid)
    return ContreeSync()


def tarball(repo: Path) -> bytes:
    """The repository as one gzipped archive.

    The SDK uploads files one at a time, and a real project is hundreds of them —
    enough to exhaust the transport timeout before the first run even starts. One
    archive is one upload.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(repo.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(repo)
            if SKIP_DIRS & set(rel.parts) or rel.name.endswith((".pyc", ".so")):
                continue
            tar.add(path, arcname=str(rel))
    return buf.getvalue()


def _parse(stdout: str) -> list[Observation]:
    """Pull the driver's payload out of stdout, ignoring anything the package printed."""
    if MARKER not in stdout:
        raise RuntimeError(f"driver produced no payload: {stdout.strip()[-300:]}")
    raw = json.loads(stdout.split(MARKER, 1)[1])
    return [
        Observation(r["input"], r["ok"], r.get("value"), r.get("error"),
                    r.get("opaque", False), r.get("type"), r.get("message"))
        for r in raw
    ]


@dataclass
class SandboxExecutor:
    base_image: str = "python:3.12-slim"
    workdir: str = "/work"
    _client_obj: object | None = field(default=None, repr=False)
    _warm: object | None = field(default=None, repr=False)
    _archive_bytes: int = 0
    # The service allows 50 concurrent instances per token; leave headroom.
    concurrency: int = 40

    @property
    def client(self):
        if self._client_obj is None:
            self._client_obj = tls.with_repair(_client)
        return self._client_obj

    def warm(
        self,
        repo: Path,
        install: tuple[str, ...] = ("pip", "install", "-q", "-e", "."),
    ) -> object:
        """Upload the repository, install it, and keep the result as a checkpoint.

        Everything expensive happens exactly once here. Every later probe forks
        this image and pays none of it again.
        """
        archive = tarball(repo)
        self._archive_bytes = len(archive)
        image = self.client.images.use(self.base_image)
        image = _check(image.run(
            "python",
            args=["-c", EXTRACT.format(dest=self.workdir)],
            files={"/tmp/repo.tgz": archive},
            disposable=False,
            timeout=600,
        ).wait(), "extract")
        self._warm = _check(image.run(
            install[0], args=list(install[1:]), cwd=self.workdir,
            disposable=False, timeout=900,
        ).wait(), " ".join(install))
        return self._warm

    @property
    def archive_bytes(self) -> int:
        return self._archive_bytes

    def observe(
        self,
        module: str,
        expressions: list[str],
        timeout: int = 300,
        overlay: dict[str, bytes] | None = None,
    ) -> list[Observation]:
        """Evaluate expressions in a fork of the warm checkpoint.

        `overlay` replaces files inside the fork only. That is how both versions
        of the code are run without building two checkpoints: one fork takes the
        repository as installed, the other takes it with the rewritten file laid
        over the top, and neither can see the other.
        """
        if self._warm is None:
            raise RuntimeError("call warm() before observe()")
        files = {"/tmp/driver.py": DRIVER.encode(),
                 "/tmp/exprs.json": json.dumps(expressions).encode()}
        for path, content in (overlay or {}).items():
            files[f"{self.workdir}/{path.lstrip('/')}"] = content
        run = _check(self._warm.run(
            "python",
            args=["/tmp/driver.py", module, "-", "/tmp/exprs.json"],
            cwd=self.workdir,
            files=files,
            env={"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1"},
            timeout=timeout,
        ).wait(), "driver")
        return _parse(run.stdout or "")

    def observe_many(
        self, module: str, batches: list[list[str]], timeout: int = 300
    ) -> list[list[Observation]]:
        """Fork once per batch, all at the same time.

        Concurrency is the entire reason to be here. `run()` is lazy but `wait()`
        performs the whole round trip, so waiting on a list of runs in sequence
        executes them in sequence — 40 forks took 50 seconds that way, exactly
        the serial rate. Driving them from a thread pool takes the same 40 forks
        to 3.7 seconds, which is 0.09s each: faster per fork than a local
        subprocess, and forty isolated microVMs rather than one shared machine.
        """
        if self._warm is None:
            raise RuntimeError("call warm() before observe_many()")
        if not batches:
            return []
        workers = max(1, min(self.concurrency, len(batches)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(
                lambda b: self.observe(module, b, timeout=timeout), batches))


def _observe_batches_docstring() -> None:  # pragma: no cover - documentation anchor
    """See SandboxExecutor.observe_many."""


def available() -> tuple[bool, str]:
    """Can this key actually use Sandboxes? Cheap enough to call before relying on it."""
    try:
        who = tls.with_repair(lambda: _client().get_token_info())
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    missing = [k for k in ("spawn", "import") if not who.permissions.get(k)]
    if missing:
        return False, (f"token lacks Sandboxes permissions ({', '.join(missing)}) — "
                       "request Beta access on the Sandboxes page in the console")
    return True, f"concurrency limit {who.limits.get('instance_max_concurrency')}"
