"""GitHub, without the git binary.

`fetch.py` shells out to `git clone`. That is the right tool on a laptop and
unavailable almost everywhere else — serverless runtimes ship a Python and not
much besides, so a deployed MUTINY could not read a repository at all.

Everything git was doing here is a read: give me this tree, give me this file,
tell me where these two commits diverged. GitHub answers all three over plain
HTTPS. A tarball is also considerably faster than a clone — django takes about
twenty seconds to clone with `--filter=blob:none` and about three to download.

Only public repositories are reachable this way, which is the same limit the
clone had. `GITHUB_TOKEN` is used when set, purely to raise the API rate limit;
nothing here needs write access and no token is required.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import ssl
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import tls
from .config import github_token
from .fetch import GITHUB, FetchError, parse

API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"

MAX_ARCHIVE_MB = 120
TIMEOUT = 120

# GitHub rejects requests without one, and a named agent is the polite form.
AGENT = "mutiny/0.1 (+https://github.com/TusharTechs/mutiny)"

SHA = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Change:
    """A change to review: two revisions, and the files that differ."""
    owner: str
    repo: str
    base: str
    head: str
    paths: tuple[str, ...]
    pr: int | None = None
    # What the change says about itself. A divergence means something quite
    # different depending on whether the author predicted it.
    title: str = ""
    body: str = ""

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}" + (f"#{self.pr}" if self.pr else "")


def _request(url: str, accept: str = "application/vnd.github+json") -> bytes:
    request = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": AGENT,
        **({"Authorization": f"Bearer {token}"} if (token := github_token()) else {}),
    })
    def open_it() -> bytes:
        # The context has to be built per call, from the environment as it
        # stands now. urllib caches a module-level opener whose SSL context was
        # fixed on first use, so a repaired bundle would otherwise be ignored on
        # the retry -- which is precisely when it matters.
        cafile = os.environ.get("SSL_CERT_FILE")
        context = ssl.create_default_context(cafile=cafile if cafile else None)
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return _read_capped(response)

    try:
        return tls.with_repair(open_it)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FetchError(f"not found on GitHub: {url.split('github.com')[-1]}") from None
        if exc.code in (403, 429):
            raise FetchError(
                "GitHub rate-limited this request. Set GITHUB_TOKEN to raise the "
                "limit, or try again in a few minutes.") from None
        raise FetchError(f"GitHub returned {exc.code} for {url}") from None
    except urllib.error.URLError as exc:
        raise FetchError(f"could not reach GitHub: {exc.reason}") from None


def _read_capped(response) -> bytes:
    """Read a response, refusing to be handed something enormous.

    Content-Length is absent on the tarball endpoint, so the cap has to be
    enforced while reading rather than before it.
    """
    limit = MAX_ARCHIVE_MB * 1024 * 1024
    buf = io.BytesIO()
    while chunk := response.read(1 << 20):
        buf.write(chunk)
        if buf.tell() > limit:
            raise FetchError(f"archive is over the {MAX_ARCHIVE_MB} MB limit")
    return buf.getvalue()


def _json(url: str) -> dict:
    return json.loads(_request(url))


def tree(owner: str, repo: str, ref: str, into: Path) -> Path:
    """Download one revision and unpack it. Returns the directory.

    GitHub wraps the archive in a single directory named for the commit, which
    is stripped: callers want a repository root, not a container.
    """
    blob = _request(f"{CODELOAD}/{owner}/{repo}/tar.gz/{ref}", accept="application/x-gzip")
    into.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        root = None
        for member in archive:
            name = member.name
            # A tar entry naming `..` or an absolute path escapes the target
            # directory. Python 3.12 warns about this; refusing is better.
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            if root is None:
                root = name.split("/")[0]
            relative = name[len(root) + 1:] if name.startswith(root + "/") else name
            if not relative:
                continue
            member.name = relative
            archive.extract(member, into, filter="data")
    return into


def pull_request(owner: str, repo: str, number: int) -> Change:
    """The two revisions a pull request sits between, and what differs.

    The comparison is against the merge base rather than the tip of the base
    branch, so other people's work on main is not attributed to this change.
    """
    pr = _json(f"{API}/repos/{owner}/{repo}/pulls/{number}")
    head_sha = pr["head"]["sha"]
    base_ref = pr["base"]["sha"]

    comparison = _json(f"{API}/repos/{owner}/{repo}/compare/{base_ref}...{head_sha}")
    merge_base = comparison.get("merge_base_commit", {}).get("sha") or base_ref
    paths = tuple(f["filename"] for f in comparison.get("files", [])
                  if f.get("filename", "").endswith(".py"))
    return Change(owner, repo, merge_base, head_sha, paths, pr=number,
                  title=(pr.get("title") or "")[:300],
                  body=(pr.get("body") or "")[:1500])


def default_branch(owner: str, repo: str) -> str:
    return _json(f"{API}/repos/{owner}/{repo}")["default_branch"]


def resolve(url: str) -> Change | tuple[str, str, str]:
    """What a URL points at, without cloning anything.

    A pull request becomes a Change. A plain repository has no change to review,
    so it comes back as (owner, repo, default branch) for the caller to rewrite
    a function in.
    """
    if not GITHUB.match(url.strip()):
        raise FetchError(
            "expected a GitHub repository or pull request URL, for example "
            "https://github.com/psf/requests or https://github.com/psf/requests/pull/1234")
    owner, repo, number = parse(url)
    if number is None:
        return owner, repo, default_branch(owner, repo)
    return pull_request(owner, repo, number)


@dataclass
class Checkout:
    """Working copies of both sides of a change, on local disk."""
    before: Path
    after: Path
    change: Change
    _root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)


def materialise(change: Change) -> Checkout:
    """Download both revisions of a change."""
    root = Path(tempfile.mkdtemp(prefix="mutiny-remote-"))
    try:
        before = tree(change.owner, change.repo, change.base, root / "before")
        after = tree(change.owner, change.repo, change.head, root / "after")
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return Checkout(before=before, after=after, change=change, _root=root)
