"""Run inside a pull request, and say something only when there is something.

A GitHub Action is the form this has to take to be used. Review happens in the
pull request; a tool that lives anywhere else is a tool somebody has to remember,
and a tool that usually has nothing to say is one they stop remembering fast.

Two rules govern the behaviour here, and both are about trust rather than
capability:

The check never fails a build. A behaviour difference is information a reviewer
weighs, not a verdict, and a bot that blocks merges on its own judgement gets
switched off within a week.

One comment per pull request, edited in place. Pushing a fix should replace the
warning, not leave it standing above a correction nobody scrolls to.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import tls
from .report import MARKER

API = "https://api.github.com"
AGENT = "mutiny/0.1 (+https://github.com/TusharTechs/mutiny)"


class CIError(RuntimeError):
    """Something about the CI environment was not as expected."""


@dataclass(frozen=True)
class Context:
    owner: str
    repo: str
    pull_request: int
    token: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.slug}/pull/{self.pull_request}"


def context(environ=None) -> Context:
    """Work out which pull request this run is about, from the runner's env."""
    env = os.environ if environ is None else environ
    slug = env.get("GITHUB_REPOSITORY", "")
    if "/" not in slug:
        raise CIError(
            "GITHUB_REPOSITORY is not set. `mutiny review-pr` expects to run "
            "inside a GitHub Action; pass --url to check a pull request from a "
            "terminal instead.")
    owner, repo = slug.split("/", 1)

    number = env.get("PR_NUMBER") or ""
    if not number:
        # The event payload is the reliable source: GITHUB_REF is absent for
        # pull_request_target and wrong for merge queues.
        path = env.get("GITHUB_EVENT_PATH")
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                number = str((payload.get("pull_request") or {}).get("number") or "")
            except (OSError, ValueError):
                number = ""
    if not number.isdigit():
        raise CIError(
            "no pull request number found. This action runs on `pull_request` "
            "events; on other triggers, pass the number as PR_NUMBER.")

    return Context(owner, repo, int(number),
                   env.get("GITHUB_TOKEN") or env.get("INPUT_GITHUB_TOKEN") or "")


def _request(url: str, token: str, method: str = "GET", body: dict | None = None):
    payload = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=payload, method=method, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": AGENT,
        "Authorization": f"Bearer {token}",
        **({"Content-Type": "application/json"} if payload else {}),
    })

    def send():
        cafile = os.environ.get("SSL_CERT_FILE")
        ctx = ssl.create_default_context(cafile=cafile if cafile else None)
        with urllib.request.urlopen(request, timeout=30, context=ctx) as response:
            return json.loads(response.read() or b"null")

    try:
        return tls.with_repair(send)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        if exc.code in (401, 403):
            raise CIError(
                f"GitHub refused the request ({exc.code}). The workflow needs "
                f"`permissions: pull-requests: write`. {detail}") from None
        raise CIError(f"GitHub returned {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise CIError(f"could not reach GitHub: {exc.reason}") from None


def existing_comment(ctx: Context) -> int | None:
    """The comment this action left last time, if it is still there."""
    comments = _request(
        f"{API}/repos/{ctx.slug}/issues/{ctx.pull_request}/comments?per_page=100",
        ctx.token) or []
    for comment in comments:
        if MARKER in (comment.get("body") or ""):
            return comment.get("id")
    return None


def publish(ctx: Context, body: str, *, speak: bool) -> str:
    """Post or edit the comment. Returns what was done, for the log.

    An existing comment is always updated, even when there is nothing to report:
    a warning that has been fixed should say so rather than stand uncorrected.
    A pull request with nothing to say and nothing said before stays untouched.
    """
    previous = existing_comment(ctx)
    if previous is None and not speak:
        return "nothing to report, and nothing said before — staying quiet"
    if previous is None:
        _request(f"{API}/repos/{ctx.slug}/issues/{ctx.pull_request}/comments",
                 ctx.token, method="POST", body={"body": body})
        return "commented"
    _request(f"{API}/repos/{ctx.slug}/issues/comments/{previous}",
             ctx.token, method="PATCH", body={"body": body})
    return "updated the existing comment"


def annotate(message: str, level: str = "error") -> None:
    """Surface a problem on the run itself, where the repository owner looks.

    Exit status stays 0 -- this tool does not fail builds -- so a workflow
    command is the only way a setup mistake becomes visible. Without it a run
    that cannot authenticate is indistinguishable from a run that found nothing,
    which is how the first live run of this action passed while doing nothing.
    """
    flattened = " ".join(str(message).split())
    print(f"::{level} title=MUTINY::{flattened}")
    summary(f"\n> **MUTINY could not run.** {flattened}\n")


def summary(text: str) -> None:
    """Write to the run summary, which needs no token and no permissions."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    except OSError:
        pass
