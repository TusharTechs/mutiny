"""The web interface: the same run, streamed to a browser.

`mutiny.session` yields events; the command line renders them as text and this
renders them as server-sent events. There is one loop, not two.

Repositories are pre-seeded rather than user-supplied. A public endpoint that
clones and installs an arbitrary URL on request is a straightforward way to be
abused, and the demo does not need it.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from mutiny.sandbox import available
from mutiny.session import verify_diff, verify_function

ROOT = Path(__file__).resolve().parent
CHECKOUTS = ROOT.parent / "experiments" / "checkouts"

# Each entry is a function worth showing: one that preserves behaviour, one that
# does not, and a real bug-fix commit reviewed as though it were a pull request.
EXAMPLES = [
    {
        "id": "cachetools-setitem",
        "repo": "cachetools",
        "title": "Cache.__setitem__",
        "blurb": "A cache insert. The rewrite reads as a tidy-up.",
        "mode": "function",
        "function": "Cache.__setitem__",
    },
    {
        "id": "semver-next-version",
        "repo": "python-semver",
        "title": "Version.next_version",
        "blurb": "Version bumping, with prerelease handling.",
        "mode": "function",
        "function": "Version.next_version",
    },
    {
        "id": "semver-prerelease-fix",
        "repo": "python-semver",
        "title": "A real bug-fix commit",
        "blurb": "Reviewed as a pull request: two functions changed.",
        "mode": "diff",
        "base": "d8813b67^",
        "head": "d8813b67",
    },
    {
        "id": "cachetools-style",
        "repo": "cachetools",
        "title": "A pure style commit",
        "blurb": "Should be silent. Nondeterminism makes that harder than it sounds.",
        "mode": "diff",
        "base": "13bb86a5^",
        "head": "13bb86a5",
    },
]

app = FastAPI(title="MUTINY", docs_url=None, redoc_url=None)


def _example(example_id: str) -> dict:
    for entry in EXAMPLES:
        if entry["id"] == example_id:
            return entry
    raise HTTPException(404, "unknown example")


@app.get("/api/examples")
def examples() -> dict:
    ok, why = available()
    return {
        "examples": [
            {k: v for k, v in e.items() if k not in {"base", "head", "function"}}
            for e in EXAMPLES
        ],
        "sandboxes": {"available": ok, "detail": why},
    }


async def _stream(example: dict, probes: int, forks: int):
    """Bridge the synchronous generator onto the event loop.

    The run is blocking and long, so it goes to a worker thread and events come
    back through a queue; otherwise the first sandbox call would stall every
    other request on the server.
    """
    repo = CHECKOUTS / example["repo"]
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def produce() -> None:
        try:
            if example["mode"] == "diff":
                events = verify_diff(repo, example["base"], example["head"],
                                     probes=probes, forks=forks)
            else:
                events = verify_function(repo, example["function"],
                                         probes=probes, forks=forks)
            for event in events:
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as exc:  # noqa: BLE001 - surface it rather than hanging
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"type": "error", "message": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.get_running_loop().run_in_executor(None, produce)
    yield f"event: open\ndata: {json.dumps(example)}\n\n"
    while True:
        event = await queue.get()
        if event is None:
            yield "event: end\ndata: {}\n\n"
            return
        yield f"data: {json.dumps(event)}\n\n"


@app.get("/api/run/{example_id}")
async def run(example_id: str, probes: int = 24, forks: int = 8):
    example = _example(example_id)
    return StreamingResponse(
        _stream(example, min(probes, 60), min(forks, 16)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
def health() -> dict:
    ok, why = available()
    return {"ok": True, "sandboxes": ok, "detail": why,
            "repos": sorted(p.name for p in CHECKOUTS.glob("*") if p.is_dir())}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
