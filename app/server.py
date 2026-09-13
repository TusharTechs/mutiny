"""The web interface: the same run, streamed to a browser.

`mutiny.session` yields events; the command line renders them as text and this
renders them as server-sent events. There is one loop, not two.

Everything here goes through GitHub over HTTPS rather than a git clone, so the
examples are the same URLs a visitor can paste in themselves -- there is no
privileged local checkout that only the demo can reach, and nothing to keep in
sync with the deployment.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mutiny import budget
from mutiny.sandbox import available
from mutiny.session import verify_url

ROOT = Path(__file__).resolve().parent

# Four things worth showing: a real bug fix whose behaviour change MUTINY finds,
# a second one on a different library, a repository whose rewrite is clean, and
# something large enough to answer "does this work on real code?".
EXAMPLES = [
    {
        "id": "semver-bump-build",
        "title": "A real bug fix, reviewed",
        "blurb": "bump_build() silently returned an unchanged version. "
                 "MUTINY finds the inputs where before and after disagree.",
        "url": "https://github.com/python-semver/python-semver/pull/480",
        "expect": "behaviour change",
    },
    {
        "id": "cachetools-maxsize",
        "title": "A guard added to a constructor",
        "blurb": "Rejecting a negative maxsize. Does it change anything else?",
        "url": "https://github.com/tkem/cachetools/pull/413",
        "expect": "behaviour change",
    },
    {
        "id": "rich-style",
        "title": "A rewrite that preserves behaviour",
        "blurb": "Nemotron rewrites Style.__str__ and MUTINY agrees it is the "
                 "same function. Silence is the harder result to earn.",
        "url": "https://github.com/Textualize/rich",
        "expect": "preserved",
    },
    {
        "id": "django",
        "title": "Django, 2932 files",
        "blurb": "Installed and executed in a Nebius sandbox, not on this server.",
        "url": "https://github.com/django/django",
        "expect": "preserved",
    },
]

app = FastAPI(title="MUTINY", docs_url=None, redoc_url=None)


def _example(example_id: str) -> dict:
    for entry in EXAMPLES:
        if entry["id"] == example_id:
            return entry
    raise HTTPException(404, "unknown example")


def _caller(request: Request) -> str:
    """Who is asking. Behind a proxy the socket address is the proxy's."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return (forwarded.split(",")[0].strip()
            or (request.client.host if request.client else "unknown"))


@app.get("/api/examples")
def examples() -> dict:
    ok, why = available()
    total = budget.spent()
    return {
        "examples": EXAMPLES,
        "sandboxes": {"available": ok, "detail": why},
        "budget": {"spent": round(total, 4), "cap": budget.TOTAL_USD,
                   "exhausted": total >= budget.TOTAL_USD},
    }


async def _stream_events(make_events):
    """Bridge a synchronous generator onto the event loop.

    The run is blocking and long, so it goes to a worker thread and events come
    back through a queue; otherwise the first sandbox call would stall every
    other request on the server.
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:
        try:
            for event in make_events():
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - surface it rather than hanging
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    loop.run_in_executor(None, produce)
    while True:
        event = await queue.get()
        if event is None:
            yield "event: end\ndata: {}\n\n"
            return
        yield f"data: {json.dumps(event)}\n\n"


def _run(url: str, caller: str, probes: int, forks: int):
    """One verification, with the deployment's spending rules applied."""
    decision = budget.check(caller)
    if not decision:
        def refused():
            yield {"type": "error", "message": decision.reason}
        return refused

    def events():
        yield {"type": "status", "stage": "fetch", "text": "fetching from GitHub"}
        spend = 0.0
        for event in verify_url(url, probes=min(probes, 40), forks=min(forks, 16),
                                cap=budget.PER_RUN_USD):
            if event.get("type") == "verdict":
                spend = float(event.get("cost") or 0)
            yield event
        budget.record(spend)
    return events


def _sse(events) -> StreamingResponse:
    return StreamingResponse(
        _stream_events(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/run-url")
async def run_url(request: Request, url: str, probes: int = 20, forks: int = 8):
    """Verify a GitHub repository or pull request the visitor supplies.

    Only github.com is accepted, the download is size-capped, and the repository
    is installed and executed inside a sandbox rather than on this machine —
    which is the reason the sandbox exists.
    """
    from mutiny.fetch import GITHUB

    if not GITHUB.match(url.strip()):
        raise HTTPException(400, (
            "expected a GitHub repository or pull request URL, for example "
            "https://github.com/psf/requests/pull/1234"))
    return _sse(_run(url, _caller(request), probes, forks))


@app.get("/api/run/{example_id}")
async def run(request: Request, example_id: str, probes: int = 24, forks: int = 8):
    example = _example(example_id)
    return _sse(_run(example["url"], _caller(request), probes, forks))


@app.get("/api/health")
def health() -> dict:
    ok, why = available()
    return {"ok": True, "sandboxes": ok, "detail": why,
            "spent": round(budget.spent(), 4), "cap": budget.TOTAL_USD}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
