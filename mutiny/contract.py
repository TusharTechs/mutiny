"""Does the documentation say which behaviour was the right one?

When MUTINY reports a behaviour change the pull request does not mention, the
reviewer's next question is not "did it change" -- that is settled, by
execution -- but "was the old behaviour promised to anybody?" A difference in an
undocumented internal helper is a curiosity. The same difference in something
the project's own documentation specifies is a broken promise.

That question is answered by text that lives outside the repository: published
documentation, changelogs, issue threads. Tavily fetches it.

The danger in asking a model about retrieved text is that it will confidently
paraphrase something the page does not say, and a citation makes that worse
rather than better -- it dresses an invention as evidence. So the model is
required to return a verbatim sentence, and that sentence is checked against the
page it supposedly came from. If it is not there, the finding is dropped
entirely. The same rule as everywhere else in this project: a claim that cannot
be checked against something observed is not reported.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import tls
from .config import load_env
from .models import SUPER, NemotronClient

ENDPOINT = "https://api.tavily.com/search"
TIMEOUT = 25

SYSTEM = """You are given published documentation for a Python library, and a
behaviour difference between two versions of one of its functions that was
measured by running both.

Decide which behaviour the documentation describes.

Reply with JSON and nothing else:

  {"states": "old" | "new" | "neither", "quote": "<one sentence, copied exactly>"}

"old" means the documentation describes what the code did BEFORE the change —
this is the answer that matters, because it means the change breaks something
the project promised its users.
"new" means it describes what the code does AFTER the change.
"neither" means the documentation does not address this behaviour at all.

A changelog line announcing that something was fixed is not a description of
behaviour. Look for text that tells a caller what the function does or returns.

The quote must be copied character for character from the text you were given.
Do not paraphrase it, do not tidy it, do not join two sentences. If no single
sentence in the text addresses this behaviour, answer "neither" with an empty
quote. Answering "neither" is expected and correct most of the time."""

USER = """Library: {slug}
Function: {qualname}

What changed, measured by running both versions:
{summary}

{witnesses}

Documentation found:
---
{documents}
---

Which behaviour does the documentation describe?"""


@dataclass(frozen=True)
class Source:
    title: str
    url: str
    content: str


@dataclass(frozen=True)
class Contract:
    """Documentation that speaks to the behaviour that changed."""
    states: str
    quote: str
    url: str
    title: str

    @property
    def contradicts(self) -> bool:
        """Does the change break what the documentation promises?"""
        return self.states == "old"


def available() -> bool:
    load_env()
    return bool(os.environ.get("TAVILY_API_KEY"))


def search(query: str, *, max_results: int = 5) -> list[Source]:
    """Published pages that might speak to this behaviour."""
    load_env()
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []

    payload = json.dumps({
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
    }).encode()
    request = urllib.request.Request(ENDPOINT, data=payload, method="POST", headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })

    def send():
        cafile = os.environ.get("SSL_CERT_FILE")
        context = ssl.create_default_context(cafile=cafile if cafile else None)
        with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as response:
            return json.loads(response.read())

    try:
        body = tls.with_repair(send)
    except (urllib.error.URLError, ValueError, TimeoutError):
        # Documentation is an escalation, never a dependency. A search that
        # fails leaves the witness exactly as strong as it was.
        return []

    return [
        Source(title=r.get("title", ""), url=r.get("url", ""),
               content=(r.get("content") or "")[:4000])
        for r in body.get("results", []) if r.get("content")
    ]


def _normalise(text: str) -> str:
    return " ".join(text.split()).lower()


def _verbatim(quote: str, sources: list[Source]) -> Source | None:
    """The page a quote actually came from, or None if it came from nowhere."""
    needle = _normalise(quote)
    # A changelog fragment like "Fixed humanize month limits." is twenty-eight
    # characters and says nothing about what the function promises. Evidence
    # that the old behaviour was specified has to be a sentence describing it.
    if len(needle) < 45:
        return None
    for source in sources:
        if needle in _normalise(source.content):
            return source
    return None


def query_for(slug: str, qualname: str, summary: str) -> str:
    package = slug.split("/")[-1].split("#")[0]
    name = qualname.split(".")[-1]
    subject = re.sub(r"[^a-zA-Z ]+", " ", summary)[:120]
    return f"{package} python documentation {qualname} {name} {subject}".strip()


def documented(
    client: NemotronClient,
    *,
    slug: str,
    qualname: str,
    summary: str,
    witnesses: list[dict],
    model: str = SUPER,
) -> Contract | None:
    """Documentation describing the behaviour that changed, if any says so."""
    sources = search(query_for(slug, qualname, summary))
    if not sources:
        return None

    documents = "\n\n".join(
        f"[{i + 1}] {s.title} — {s.url}\n{s.content}" for i, s in enumerate(sources))
    shown = "\n".join(
        f"{w['input']}\n  before: {w['before']}\n  after:  {w['after']}"
        for w in witnesses[:3])

    try:
        text, _ = client.complete(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": USER.format(
                 slug=slug, qualname=qualname, summary=summary or "(not summarised)",
                 witnesses=shown, documents=documents[:12000])}],
            model=model, max_tokens=2000, temperature=0.0,
            tag=f"contract:{qualname}",
        )
    except Exception:  # noqa: BLE001 - an escalation, never a dependency
        return None

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        answer = json.loads(match.group(0))
    except ValueError:
        return None

    states = str(answer.get("states", "")).strip().lower()
    quote = str(answer.get("quote", "")).strip()
    if states not in ("old", "new") or not quote:
        return None

    source = _verbatim(quote, sources)
    if source is None:
        # The sentence is not on any page we fetched. Reporting it would dress
        # an invention as a citation, which is worse than saying nothing.
        return None
    return Contract(states=states, quote=quote, url=source.url, title=source.title)
