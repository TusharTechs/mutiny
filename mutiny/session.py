"""The verification loop, as a stream of events.

The command line and the web interface are two renderings of the same run, so
the loop lives here and yields what happened rather than printing it. Anything
that wants to show progress — a terminal, a browser over server-sent events, a
CI annotation — consumes the same sequence.

Events are plain dictionaries with a ``type`` key, because they end up as JSON
on a wire more often than not.
"""
from __future__ import annotations

import difflib
import shutil
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import refactor as refactor_mod
from . import remote
from . import review as review_mod
from .diff import hunk_between
from .differential import Observation, compare, confirm
from .differential import observe as local_observe
from .explain import explain
# fetch clones with the git binary, which a serverless runtime does not have.
# It stays for local experiments; everything reached from a URL goes via remote.
from .fetch import FetchError
from .inputs import generate_validated, is_stateful, receiver_candidates
from .models import NemotronClient
from .sandbox import SandboxExecutor, available, file_at, tarball, tarball_at
from .source import focused_module, function_span

Event = dict[str, Any]


def _event(kind: str, **fields: Any) -> Event:
    return {"type": kind, **fields}


def _split(items: list[str], into: int) -> list[list[str]]:
    """Deal the probes across forks, round-robin.

    Round-robin rather than contiguous chunks so no single fork inherits all the
    slow or all the malformed probes, which would leave the others idle.
    """
    into = max(1, min(into, len(items)))
    buckets: list[list[str]] = [[] for _ in range(into)]
    for i, item in enumerate(items):
        buckets[i % into].append(item)
    return [b for b in buckets if b]


def _unified(before: str, after: str) -> list[str]:
    return [
        line for line in difflib.unified_diff(
            before.splitlines(), after.splitlines(), lineterm="", n=3)
        if not line.startswith(("+++", "---"))
    ]


class Runner:
    """Executes probes either in Sandboxes or locally, reporting per-fork progress."""

    def __init__(self, executor: SandboxExecutor | None, repo: Path, python: str):
        self.executor = executor
        self.repo = repo
        self.python = python

    @property
    def forked(self) -> bool:
        return self.executor is not None

    def run(self, module: str, probes: list[str],
            overlay: dict[str, bytes] | None = None) -> list[Observation]:
        if self.executor is not None:
            return self.executor.observe(module, probes, overlay=overlay)
        return local_observe(self.repo, module, probes,
                             self.python, baseline=overlay is None)

    def run_forked(
        self, module: str, probes: list[str], overlay: dict[str, bytes] | None,
        side: str, forks: int,
    ) -> Iterator[tuple[Event, list[Observation] | None]]:
        """Yield a progress event per fork, then the collected observations.

        Concurrency is the point of being in Sandboxes at all: run() is lazy but
        wait() performs the whole round trip, so a sequential wait is a
        sequential run.
        """
        batches = _split(probes, forks)
        if self.executor is None:
            yield _event("fork", side=side, index=0, total=1, state="running"), None
            observations = self.run(module, probes, overlay)
            yield _event("fork", side=side, index=0, total=1, state="done"), None
            yield _event("side_done", side=side), observations
            return

        for i in range(len(batches)):
            yield _event("fork", side=side, index=i, total=len(batches),
                         state="running"), None

        collected: list[Observation] = []
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            futures = {
                pool.submit(self.run, module, batch, overlay): i
                for i, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    observations = future.result()
                except Exception as exc:  # noqa: BLE001 - one fork must not kill the run
                    yield _event("fork", side=side, index=index, total=len(batches),
                                 state="failed", detail=str(exc)[:160]), None
                    continue
                collected += observations
                yield _event("fork", side=side, index=index, total=len(batches),
                             state="done", probes=len(observations)), None
        yield _event("side_done", side=side), collected


def verify_function(
    repo: Path,
    qualname: str,
    *,
    model: str = "nvidia/nemotron-3-super-120b-a12b",
    probes: int = 30,
    forks: int = 8,
    cap: float = 5.0,
    use_sandbox: bool = True,
    python: str | None = None,
    executor: SandboxExecutor | None = None,
) -> Iterator[Event]:
    """Rewrite a function with Nemotron, then check the rewrite preserved behaviour."""
    import sys

    python = python or sys.executable
    started = time.monotonic()

    located = _locate(repo, qualname)
    if located is None:
        yield _event("error", message=f"could not find {qualname!r} in {repo.name}")
        return
    path, module = located
    rel = str(path.relative_to(repo))
    yield _event("target", repo=repo.name, path=rel, function=qualname, module=module)

    client = NemotronClient(cap_usd=cap)
    opening = client.ledger.total_usd
    source = path.read_text(encoding="utf-8")

    yield _event("status", stage="rewrite", text=f"rewriting with {model.split('/')[-1]}")
    rewrite = refactor_mod.refactor(client, source, qualname, model=model)
    if rewrite is None or not rewrite.changed:
        yield _event("error", message="the model returned no usable rewrite")
        return
    patched = refactor_mod.apply(source, qualname, rewrite.rewritten)
    if patched is None:
        yield _event("error", message="the rewrite did not apply cleanly")
        return
    applied = _as_applied(patched, qualname) or rewrite.rewritten
    yield _event("diff", lines=_unified(rewrite.original, applied))

    yield from _probe_and_compare(
        client=client, repo=repo, module=module, qualname=qualname,
        base_source=source, overlay={rel: patched.encode()},
        diff_text="\n".join(_unified(rewrite.original, applied)),
        probes=probes, forks=forks, use_sandbox=use_sandbox, python=python,
        started=started, opening=opening, archive=None, executor=executor,
    )


def verify_diff(
    repo: Path,
    base: str,
    head: str,
    *,
    probes: int = 30,
    forks: int = 8,
    cap: float = 5.0,
    max_functions: int = 10,
    python: str | None = None,
) -> Iterator[Event]:
    """Check a change that already exists — a branch, a pull request, a commit."""
    import sys

    python = python or sys.executable
    started = time.monotonic()
    try:
        review = review_mod.plan(repo, base, head, max_targets=max_functions)
    except ValueError as exc:
        yield _event("error", message=str(exc))
        return

    yield from _verify_review(
        review, repo_name=repo.name, deps_repo=repo,
        archive=lambda: tarball_at(repo, review.base),
        head_bytes=lambda path: file_at(repo, review.head, path),
        hunk_text=lambda path: review_mod.hunk(repo, review.base, review.head, path),
        probes=probes, forks=forks, cap=cap, python=python, started=started,
    )


def verify_trees(
    before: Path,
    after: Path,
    *,
    base: str = "before",
    head: str = "after",
    paths: tuple[str, ...] | None = None,
    name: str = "",
    probes: int = 30,
    forks: int = 8,
    cap: float = 5.0,
    max_functions: int = 10,
    python: str | None = None,
) -> Iterator[Event]:
    """Check a change that arrives as two directories rather than two git refs.

    This is what a deployed MUTINY runs: serverless runtimes have no git binary,
    so both revisions are downloaded and unpacked, and everything downstream is
    identical.
    """
    import sys

    python = python or sys.executable
    started = time.monotonic()
    review = review_mod.plan_between(before, after, base, head, paths,
                                     max_targets=max_functions)
    yield from _verify_review(
        review, repo_name=name or after.name, deps_repo=before,
        archive=lambda: tarball(before),
        head_bytes=lambda path: (after / path).read_bytes(),
        hunk_text=lambda path: hunk_between(before, after, path),
        probes=probes, forks=forks, cap=cap, python=python, started=started,
    )


def _verify_review(
    review,
    *,
    repo_name: str,
    deps_repo: Path,
    archive,
    head_bytes,
    hunk_text,
    probes: int,
    forks: int,
    cap: float,
    python: str,
    started: float,
) -> Iterator[Event]:
    """Everything a reviewed change does once its two revisions are readable.

    Both entry points reach the same engine; they differ only in how they answer
    "give me this file at that revision".
    """
    yield _event("review", repo=repo_name, base=review.base[:8], head=review.head[:8],
                 targets=[t.label for t in review.targets],
                 skipped=[{"path": p, "why": w} for p, w in review.skipped])
    if not review.targets:
        yield _event("verdict", changed=False, functions=0, findings=0,
                     seconds=round(time.monotonic() - started, 1), cost=0.0)
        return

    client = NemotronClient(cap_usd=cap)
    opening = client.ledger.total_usd
    ok, why = available()
    if not ok:
        yield _event("error", message=f"verify-diff needs Sandboxes: {why}")
        return

    yield _event("status", stage="checkpoint",
                 text=f"warming a checkpoint at {review.base[:8]}")
    executor = SandboxExecutor()
    executor.warm_archive(archive(), deps_repo)
    yield _event("checkpoint", seconds=round(time.monotonic() - started, 1),
                 kib=executor.archive_bytes // 1024, mode=executor.install_mode)

    findings = 0
    for target in review.targets:
        yield _event("function", name=target.qualname, path=target.path)
        overlay = {target.path: head_bytes(target.path)}
        result: dict[str, Any] = {}
        for event in _probe_and_compare(
            client=client, repo=deps_repo, module=target.module, qualname=target.qualname,
            base_source=target.base_source, overlay=overlay,
            diff_text=hunk_text(target.path),
            probes=probes, forks=forks, use_sandbox=True, python=python,
            started=started, opening=opening, archive=None, executor=executor,
            emit_verdict=False, sink=result,
        ):
            yield event
        if result.get("divergences"):
            findings += 1

    yield _event("verdict", changed=bool(findings), functions=len(review.targets),
                 findings=findings, seconds=round(time.monotonic() - started, 1),
                 cost=round(client.ledger.total_usd - opening, 4))


def verify_url(
    url: str,
    *,
    probes: int = 30,
    forks: int = 8,
    cap: float = 5.0,
    max_functions: int = 6,
    keep: bool = False,
) -> Iterator[Event]:
    """Verify whatever a GitHub URL points at.

    A pull request is checked against its merge base. A plain repository has no
    change to review, so the most heavily branched function is rewritten and that
    rewrite is checked instead — which is the same question asked of code the
    visitor chose rather than code we chose.
    """
    yield _event("status", stage="fetch", text=f"fetching {url}")
    try:
        target = remote.resolve(url)
    except FetchError as exc:
        yield _event("error", message=str(exc))
        return

    if isinstance(target, remote.Change):
        yield _event("fetched", slug=target.slug, pull_request=True)
        try:
            checkout = remote.materialise(target)
        except FetchError as exc:
            yield _event("error", message=str(exc))
            return
        try:
            yield from verify_trees(
                checkout.before, checkout.after,
                base=target.base, head=target.head, paths=target.paths,
                name=f"{target.owner}/{target.repo}",
                probes=probes, forks=forks, cap=cap, max_functions=max_functions)
        finally:
            if not keep:
                checkout.cleanup()
        return

    owner, repo, branch = target
    yield _event("fetched", slug=f"{owner}/{repo}", pull_request=False)
    root = Path(tempfile.mkdtemp(prefix="mutiny-repo-"))
    try:
        tree = remote.tree(owner, repo, branch, root / repo)
    except FetchError as exc:
        shutil.rmtree(root, ignore_errors=True)
        yield _event("error", message=str(exc))
        return
    try:
        yield from _verify_repository(tree, f"{owner}/{repo}", probes=probes,
                                      forks=forks, cap=cap, attempts=3)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def _verify_repository(
    path: Path, slug: str, *, probes: int, forks: int, cap: float, attempts: int = 3,
) -> Iterator[Event]:
    """Rewrite and check a function of a repository, moving on if one is unprobeable.

    One sandbox is warmed for the whole walk, so falling back to a second target
    costs a rewrite and a round of probe generation, not another install.
    """
    candidates = _candidate_functions(path, limit=attempts)
    if not candidates:
        yield _event("error",
                     message=f"no function in {slug} was suitable to rewrite")
        return

    started = time.monotonic()
    executor = None
    ok, why = available()
    if ok:
        yield _event("status", stage="checkpoint", text="warming a sandbox checkpoint")
        try:
            executor = SandboxExecutor()
            executor.warm(path)
            yield _event("checkpoint", seconds=round(time.monotonic() - started, 1),
                         kib=executor.archive_bytes // 1024, mode=executor.install_mode)
        except Exception as exc:  # noqa: BLE001 - local execution is the fallback
            executor = None
            yield _event("status", stage="checkpoint",
                         text=f"sandbox unavailable ({type(exc).__name__}); running locally")

    for index, target in enumerate(candidates):
        probed = True
        for event in verify_function(path, target, probes=probes,
                                     forks=forks, cap=cap, executor=executor):
            if event.get("type") == "no_probes":
                probed = False
                remaining = len(candidates) - index - 1
                yield _event(
                    "status", stage="probes",
                    text=(f"no input could be constructed for {target}"
                          + (f"; trying {candidates[index + 1]}" if remaining else "")))
                continue
            yield event
        if probed:
            return

    yield _event("verdict", changed=False, functions=0, findings=0,
                 seconds=round(time.monotonic() - started, 1), cost=0.0)
    yield _event("error", message=(
        f"none of the {len(candidates)} busiest functions in {slug} could be "
        "reached by a generated input — they take objects that need real setup. "
        "Point MUTINY at a pull request, or name a function with --function."))


def _busiest_function(repo: Path, ceiling: int = 70) -> str | None:
    """The most heavily branched function, as something worth rewriting."""
    ranked = _candidate_functions(repo, ceiling=ceiling, limit=1)
    return ranked[0] if ranked else None


def _candidate_functions(repo: Path, ceiling: int = 70, limit: int = 6) -> list[str]:
    """Functions worth rewriting, most heavily branched first.

    The busiest function in a large framework is often the least reachable one:
    django, sqlalchemy and requests all put their densest branching behind objects
    that take a paragraph of setup to build, and no probe generator gets there.
    Returning a ranked list lets the caller move down it when the top choice
    produces nothing, instead of reporting an empty result for the repository.
    """
    import ast

    skip = {".git", ".venv", "tests", "test", "build", "dist", "docs", "examples"}
    found: list[tuple[int, str]] = []
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if skip & set(rel.parts) or rel.name.startswith(("test_", "setup", "conf")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        def consider(node, qualname: str) -> None:
            lines = (node.end_lineno or node.lineno) - node.lineno
            if not (5 <= lines <= ceiling):
                return
            branches = sum(1 for n in ast.walk(node)
                           if isinstance(n, (ast.If, ast.For, ast.While, ast.Try)))
            if branches:
                found.append((branches, qualname))

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consider(node, node.name)
            elif isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        consider(child, f"{node.name}.{child.name}")

    ordered, seen = [], set()
    for _, qualname in sorted(found, key=lambda pair: -pair[0]):
        if qualname not in seen:
            seen.add(qualname)
            ordered.append(qualname)
    return ordered[:limit]


def _probe_and_compare(
    *, client, repo, module, qualname, base_source, overlay, diff_text,
    probes, forks, use_sandbox, python, started, opening, archive,
    executor: SandboxExecutor | None = None, emit_verdict: bool = True,
    sink: dict[str, Any] | None = None,
) -> Iterator[Event]:
    if executor is None and use_sandbox:
        ok, why = available()
        if ok:
            yield _event("status", stage="checkpoint", text="warming a sandbox checkpoint")
            executor = SandboxExecutor()
            executor.warm(repo)
            yield _event("checkpoint", seconds=round(time.monotonic() - started, 1),
                         kib=executor.archive_bytes // 1024, mode=executor.install_mode)
        else:
            yield _event("status", stage="checkpoint",
                         text=f"sandboxes unavailable ({why}); running locally")

    runner = Runner(executor, repo, python)

    yield _event("status", stage="probes", text="generating probes")
    generated, _ = generate_validated(
        client, module, qualname, focused_module(base_source, qualname),
        probe=lambda e: runner.run(module, e), n=probes,
        subclasses=receiver_candidates(base_source, qualname),
        stateful=is_stateful(base_source, qualname),
        diff=diff_text,
    )
    if not generated:
        yield _event("no_probes", function=qualname)
        if sink is not None:
            sink["divergences"] = []
        return
    yield _event("probes", items=generated[:60], count=len(generated))

    sides: dict[str, list[Observation]] = {}
    for side, side_overlay in (("before", None), ("after", overlay)):
        for event, collected in runner.run_forked(
            module, generated, side_overlay, side, forks
        ):
            if collected is None:
                yield event
            else:
                sides[side] = collected
                yield event

    before, after = sides.get("before", []), sides.get("after", [])
    divergences, flaky = confirm(
        compare(before, after),
        run_before=lambda e: runner.run(module, e),
        run_after=lambda e: runner.run(module, e, overlay),
    )
    if flaky:
        yield _event("flaky", count=len(flaky))

    index = {o.input: o for o in after}
    agreed = sum(1 for b in before if b.ok and index.get(b.input) and index[b.input].ok)
    payload = [
        {"input": d.input,
         "before": d.before.value if d.before.ok else d.before.error,
         "after": d.after.value if d.after.ok else d.after.error,
         "kind": d.kind}
        for d in divergences
    ]
    summary = ""
    if payload:
        yield _event("status", stage="explain", text="describing the change")
        summary = explain(client, qualname, diff_text, payload).text

    if sink is not None:
        sink["divergences"] = payload
    yield _event("result", function=qualname, divergences=payload,
                 agreed=agreed, probes=len(generated), summary=summary)

    if emit_verdict:
        yield _event("verdict", changed=bool(divergences), functions=1,
                     findings=1 if divergences else 0,
                     seconds=round(time.monotonic() - started, 1),
                     cost=round(client.ledger.total_usd - opening, 4))


SKIP = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules"}


def _locate(repo: Path, qualname: str) -> tuple[Path, str] | None:
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if SKIP & set(rel.parts) or "tests" in rel.parts or rel.name.startswith("test_"):
            continue
        try:
            function_span(path.read_text(encoding="utf-8"), qualname)
        except (ValueError, SyntaxError, UnicodeDecodeError, OSError):
            continue
        return path, review_mod.module_name(str(rel))
    return None


def _as_applied(patched: str, qualname: str) -> str | None:
    try:
        lo, hi = function_span(patched, qualname)
    except (ValueError, SyntaxError):
        return None
    return "\n".join(patched.splitlines()[lo - 1 : hi])
