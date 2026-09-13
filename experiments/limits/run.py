#!/usr/bin/env python
"""Where does MUTINY stop working?

Everything measured so far has been on small, tidy libraries. This walks up the
size curve — requests, flask, rich, sqlalchemy, django — and records which stage
fails first and how long each one takes, rather than waiting for a judge to
find out.

Each stage is timed and failures are caught per stage, so a repository that dies
at `warm` still reports what fetch and refactor cost.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mutiny import refactor as refactor_mod
from mutiny.differential import compare, confirm
from mutiny.fetch import FetchError, cleanup, fetch
from mutiny.inputs import generate_validated, is_stateful, receiver_candidates
from mutiny.models import NemotronClient
from mutiny.sandbox import SandboxExecutor, tarball
from mutiny.session import _candidate_functions, _locate
from mutiny.source import focused_module

ROOT = Path(__file__).resolve().parent

REPOS = [
    "https://github.com/pallets/flask",
    "https://github.com/psf/requests",
    "https://github.com/Textualize/rich",
    "https://github.com/sqlalchemy/sqlalchemy",
    "https://github.com/django/django",
]


@dataclass
class Stage:
    name: str
    seconds: float = 0.0
    ok: bool = False
    detail: str = ""


@dataclass
class Result:
    url: str
    slug: str = ""
    python_files: int = 0
    source_mb: float = 0.0
    archive_kb: int = 0
    function: str = ""
    attempts: int = 0
    probes: int = 0
    divergent: int = 0
    stages: list = field(default_factory=list)
    failed_at: str = ""

    def add(self, stage: Stage) -> None:
        self.stages.append(asdict(stage))
        if not stage.ok and not self.failed_at:
            self.failed_at = stage.name


def timed(result: Result, name: str, fn):
    """Run one stage, recording what it cost and whether it survived."""
    stage = Stage(name)
    start = time.monotonic()
    try:
        value = fn()
        stage.ok = True
        return value
    except Exception as exc:  # noqa: BLE001 - the point is to record the failure
        stage.detail = f"{type(exc).__name__}: {exc}"[:220]
        return None
    finally:
        stage.seconds = round(time.monotonic() - start, 1)
        result.add(stage)
        mark = "ok  " if stage.ok else "FAIL"
        print(f"    {mark} {name:<12} {stage.seconds:7.1f}s"
              + (f"  {stage.detail}" if stage.detail else ""), flush=True)


def run(url: str, client: NemotronClient) -> Result:
    result = Result(url=url)
    print(f"\n{url}", flush=True)

    source = timed(result, "fetch", lambda: fetch(url))
    if source is None:
        return result
    result.slug = source.slug

    try:
        files = [p for p in source.path.rglob("*.py") if ".git" not in p.parts]
        result.python_files = len(files)
        result.source_mb = round(
            sum(p.stat().st_size for p in files) / (1024 * 1024), 1)
        print(f"    {result.python_files} python files, {result.source_mb} MB",
              flush=True)

        candidates = timed(result, "locate",
                           lambda: _candidate_functions(source.path, limit=3))
        if not candidates:
            return result

        archive = timed(result, "archive", lambda: tarball(source.path))
        if archive is None:
            return result
        if archive is None:
            return result
        result.archive_kb = len(archive) // 1024
        print(f"    archive {result.archive_kb} KiB", flush=True)

        executor = SandboxExecutor()
        warmed = timed(result, "warm", lambda: executor.warm(source.path))
        if warmed is None:
            return result
        print(f"    {executor.install_mode}", flush=True)

        # Walk down the ranked candidates: the busiest function in a framework is
        # often the one needing the most setup, and no probe reaches it.
        probes, target, path, module, rewrite, patched = None, "", None, "", None, None
        for candidate in candidates:
            located = _locate(source.path, candidate)
            if located is None:
                continue
            path, module = located
            text = path.read_text(encoding="utf-8")
            target, result.attempts = candidate, result.attempts + 1
            print(f"    try {result.attempts}: {candidate}  ({module})", flush=True)

            rewrite = timed(result, "refactor",
                            lambda: refactor_mod.refactor(client, text, candidate))
            if rewrite is None:
                continue
            patched = refactor_mod.apply(text, candidate, rewrite.rewritten)
            if patched is None:
                continue

            probes = timed(result, "probes", lambda: generate_validated(
                client, module, candidate, focused_module(text, candidate),
                probe=lambda e: executor.observe(module, e), n=24,
                subclasses=receiver_candidates(text, candidate),
                stateful=is_stateful(text, candidate),
                diff="\n".join(__import__("difflib").unified_diff(
                    rewrite.original.splitlines(), rewrite.rewritten.splitlines(),
                    lineterm="", n=4)))[0])
            if probes:
                break
            print("    (no probes; next candidate)", flush=True)

        result.function = target
        if not probes:
            result.failed_at = result.failed_at or "probes"
            return result
        result.probes = len(probes)
        rel = str(path.relative_to(source.path))

        def both():
            before = executor.observe(module, probes)
            after = executor.observe(module, probes, overlay={rel: patched.encode()})
            found, _ = confirm(
                compare(before, after),
                run_before=lambda e: executor.observe(module, e),
                run_after=lambda e: executor.observe(module, e,
                                                     overlay={rel: patched.encode()}))
            return found

        divergences = timed(result, "compare", both)
        result.divergent = len(divergences or [])
    finally:
        cleanup(source)
    return result


def main() -> int:
    client = NemotronClient(cap_usd=20.0)
    opening = client.ledger.total_usd
    results = []
    out = ROOT / "results" / f"limits-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)

    for url in REPOS:
        try:
            results.append(run(url, client))
        except Exception:
            print("    unexpected: " + traceback.format_exc().splitlines()[-1][:150])
        out.write_text(json.dumps([asdict(r) for r in results], indent=2))

    print(f"\n\n{'=' * 78}\nLIMITS\n{'=' * 78}")
    print(f"{'repo':<22}{'files':>7}{'MB':>6}{'archive':>9}{'tries':>7}"
          f"{'probes':>8}{'div':>5}{'total':>8}  failed at")
    print("-" * 78)
    for r in results:
        total = sum(s["seconds"] for s in r.stages)
        print(f"{(r.slug or r.url.split('/')[-1]):<22}{r.python_files:>7}"
              f"{r.source_mb:>6}{r.archive_kb:>8}K{r.attempts:>7}{r.probes:>8}"
              f"{r.divergent:>5}"
              f"{total:>7.0f}s  {r.failed_at or '—'}")
    print("-" * 78)
    print(f"  spend: ${client.ledger.total_usd - opening:.4f}")
    print(f"\n  written: {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
