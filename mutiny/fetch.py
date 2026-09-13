"""Turn a GitHub URL into something verifiable.

Accepts a repository or a pull request:

    https://github.com/owner/repo
    https://github.com/owner/repo/pull/123

A pull request is fetched from ``refs/pull/N/head`` and compared against its
merge base with the default branch, so other people's work on main is not
attributed to the change under review.

Clones use ``--filter=blob:none``: full history, because merge-base needs it,
without paying for every blob ever committed.

Nothing here installs or executes the repository. That happens inside a sandbox,
which is the entire reason the sandbox is there — this module only obtains
bytes.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

GITHUB = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9][\w.-]{0,38})/"
    r"(?P<repo>[\w.-]{1,100}?)(?:\.git)?"
    # /pull/7/files and /pull/7/commits are what you get from the tabs a
    # reviewer actually has open, so accept anything trailing the number.
    r"(?:/pull/(?P<pr>\d{1,7})(?:/[\w.-]*)*)?"
    r"/?$"
)

MAX_MB = 250
CLONE_TIMEOUT = 300


class FetchError(RuntimeError):
    """The URL could not be turned into a working copy."""


@dataclass(frozen=True)
class Source:
    path: Path
    owner: str
    repo: str
    pr: int | None
    base: str | None
    head: str | None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}" + (f"#{self.pr}" if self.pr else "")

    @property
    def is_pull_request(self) -> bool:
        return self.pr is not None


def parse(url: str) -> tuple[str, str, int | None]:
    """(owner, repo, pull request number or None). Only github.com is accepted."""
    match = GITHUB.match(url.strip())
    if not match:
        raise FetchError(
            "expected a GitHub repository or pull request URL, for example "
            "https://github.com/psf/requests or https://github.com/psf/requests/pull/1234")
    pr = match.group("pr")
    return match.group("owner"), match.group("repo"), int(pr) if pr else None


def _git(args: list[str], cwd: Path | None = None, timeout: int = CLONE_TIMEOUT) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
        env={"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true",
             "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )
    if result.returncode != 0:
        raise FetchError(f"git {args[0]} failed: {result.stderr.strip()[:240]}")
    return result.stdout


def _size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def fetch(url: str, into: Path | None = None) -> Source:
    """Clone the repository, and the pull request if the URL named one."""
    owner, repo, pr = parse(url)
    target = Path(into or tempfile.mkdtemp(prefix="mutiny-fetch-")) / repo
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    remote = f"https://github.com/{owner}/{repo}.git"
    try:
        _git(["clone", "--filter=blob:none", "--quiet", remote, str(target)])
    except FetchError as exc:
        raise FetchError(
            f"could not clone {owner}/{repo} — is it public and spelled correctly? "
            f"({exc})") from None

    size = _size_mb(target)
    if size > MAX_MB:
        shutil.rmtree(target, ignore_errors=True)
        raise FetchError(f"{owner}/{repo} is {size:.0f} MB, over the {MAX_MB} MB limit")

    if pr is None:
        return Source(target, owner, repo, None, None, None)

    try:
        _git(["fetch", "--quiet", "origin", f"pull/{pr}/head:mutiny/pr-{pr}"], cwd=target)
    except FetchError:
        shutil.rmtree(target, ignore_errors=True)
        raise FetchError(
            f"pull request #{pr} was not found in {owner}/{repo}") from None

    default = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                   cwd=target).strip() or "origin/main"
    head = f"mutiny/pr-{pr}"
    base = _git(["merge-base", default, head], cwd=target).strip()
    if not base:
        raise FetchError(f"pull request #{pr} shares no history with {default}")
    return Source(target, owner, repo, pr, base, head)


def cleanup(source: Source) -> None:
    """Remove a working copy obtained by fetch()."""
    root = source.path.parent
    if root.name.startswith("mutiny-fetch-"):
        shutil.rmtree(root, ignore_errors=True)
    else:
        shutil.rmtree(source.path, ignore_errors=True)
