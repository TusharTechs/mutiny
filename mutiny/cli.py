"""`mutiny verify` — did that rewrite preserve behaviour?

One command: Nemotron rewrites a function, both versions run on the same
generated inputs inside forks of a single warm Sandboxes checkpoint, and the
answer is either the exact input where they disagree or a clean bill.

The two versions share one checkpoint. The rewritten file is laid over the top
inside its own fork, so nothing expensive is built twice and neither side can
see the other.
"""
from __future__ import annotations

import argparse
import difflib
import sys
import time
from pathlib import Path

from . import refactor as refactor_mod
from . import review as review_mod
from .differential import compare, confirm
from .differential import observe as local_observe
from .inputs import generate_validated, is_stateful, receiver_candidates
from .models import NemotronClient
from .sandbox import SandboxExecutor, available, file_at, tarball_at
from .source import focused_module, function_span

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m")

SKIP = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules"}


def _palette(enabled: bool) -> tuple[str, ...]:
    return (BOLD, DIM, RED, GREEN, YELLOW, RESET) if enabled else ("",) * 6


def locate(repo: Path, qualname: str) -> tuple[Path, str] | None:
    """Find the file defining `qualname`, and the name it is imported under."""
    for path in sorted(repo.rglob("*.py")):
        rel = path.relative_to(repo)
        if SKIP & set(rel.parts) or "tests" in rel.parts or rel.name.startswith("test_"):
            continue
        try:
            function_span(path.read_text(encoding="utf-8"), qualname)
        except (ValueError, SyntaxError, UnicodeDecodeError, OSError):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[0] in {"src", "lib"}:
            parts = parts[1:]
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return path, ".".join(parts)
    return None


def _as_applied(patched: str, qualname: str) -> str | None:
    """The rewritten function exactly as it now sits in the file."""
    try:
        lo, hi = function_span(patched, qualname)
    except (ValueError, SyntaxError):
        return None
    return "\n".join(patched.splitlines()[lo - 1 : hi])


def show_diff(before: str, after: str, dim: str, red: str, green: str, reset: str) -> None:
    for line in difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="original", tofile="rewritten", lineterm="", n=2,
    ):
        if line.startswith(("+++", "---")):
            continue
        colour = green if line.startswith("+") else red if line.startswith("-") else dim
        print(f"    {colour}{line}{reset}")


def verify(args: argparse.Namespace) -> int:
    bold, dim, red, green, yellow, reset = _palette(
        not args.no_color and sys.stdout.isatty())
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"{red}no such repository: {repo}{reset}", file=sys.stderr)
        return 2

    found = locate(repo, args.function)
    if found is None:
        print(f"{red}could not find {args.function!r} in {repo.name} — qualify it "
              f"as Class.method if the name repeats{reset}", file=sys.stderr)
        return 2
    path, module = found
    rel = str(path.relative_to(repo))
    print(f"{bold}{repo.name}{reset}  {rel}  {bold}{args.function}{reset}"
          f"  {dim}({module}){reset}\n")

    client = NemotronClient(cap_usd=args.cap)
    opening_spend = client.ledger.total_usd
    source = path.read_text(encoding="utf-8")

    print(f"{dim}rewriting with {args.model.split('/')[-1]}...{reset}")
    rewrite = refactor_mod.refactor(client, source, args.function, model=args.model)
    if rewrite is None or not rewrite.changed:
        print(f"{yellow}the model returned no usable rewrite{reset}")
        return 1
    patched = refactor_mod.apply(source, args.function, rewrite.rewritten)
    if patched is None:
        print(f"{yellow}the rewrite did not apply cleanly{reset}")
        return 1

    # Diff against the rewrite *as applied*. The model replies at module
    # indentation and apply() re-indents it, so diffing the raw reply marks every
    # single line as changed and buries the actual edit.
    applied = _as_applied(patched, args.function) or rewrite.rewritten
    show_diff(rewrite.original, applied, dim, red, green, reset)

    use_sandbox = not args.local
    if use_sandbox:
        ok, why = available()
        if not ok:
            print(f"\n{yellow}sandboxes unavailable ({why}); running locally{reset}")
            use_sandbox = False

    executor = None
    started = time.monotonic()
    if use_sandbox:
        print(f"\n{dim}warming a sandbox checkpoint...{reset}")
        executor = SandboxExecutor()
        executor.warm(repo)
        print(f"{dim}  ready in {time.monotonic() - started:.1f}s, "
              f"{executor.archive_bytes // 1024} KiB uploaded, "
              f"{executor.install_mode}{reset}")
        if executor.install_note:
            first = executor.install_note.splitlines()[0] if executor.install_note else ""
            print(f"{dim}  (not installable as a package: {first[:80]}){reset}")

    def probe(expressions: list[str]):
        if executor is not None:
            return executor.observe(module, expressions)
        return local_observe(repo, module, expressions, args.python)

    print(f"{dim}generating probes...{reset}")
    diff_text = "\n".join(difflib.unified_diff(
        rewrite.original.splitlines(), rewrite.rewritten.splitlines(),
        lineterm="", n=4))
    expressions, _ = generate_validated(
        client, module, args.function, rewrite.original, probe=probe,
        n=args.probes,
        subclasses=receiver_candidates(source, args.function),
        stateful=is_stateful(source, args.function),
        diff=diff_text,
    )
    if not expressions:
        print(f"{yellow}no usable probes were generated{reset}")
        return 1
    print(f"{dim}  {len(expressions)} probes{reset}")

    print(f"{dim}running both versions...{reset}")
    if executor is not None:
        before = executor.observe(module, expressions)
        after = executor.observe(module, expressions, overlay={rel: patched.encode()})
    else:
        before = local_observe(repo, module, expressions, args.python)
        original_bytes = path.read_bytes()
        path.write_text(patched, encoding="utf-8")
        try:
            after = local_observe(repo, module, expressions, args.python, baseline=False)
        finally:
            path.write_bytes(original_bytes)

    if executor is not None:
        divergences, flaky = confirm(
            compare(before, after),
            run_before=lambda e: executor.observe(module, e),
            run_after=lambda e: executor.observe(module, e, overlay={rel: patched.encode()}),
        )
    else:
        divergences, flaky = compare(before, after), []
    if flaky:
        print(f"{dim}{len(flaky)} probe(s) disagreed once but not on repeat — "
              f"discarded as nondeterministic{reset}")
    elapsed = time.monotonic() - started
    spend = client.ledger.total_usd - opening_spend

    print()
    if not divergences:
        index = {o.input: o for o in after}
        agreed = sum(1 for b in before
                     if b.ok and index.get(b.input) and index[b.input].ok)
        print(f"{green}{bold}Behaviour preserved.{reset}  {agreed} of "
              f"{len(expressions)} probes ran on both versions and agreed on every one.")
        print(f"{dim}{elapsed:.1f}s, ${spend:.4f}{reset}")
        return 0

    print(f"{red}{bold}Behaviour changed.{reset}  {len(divergences)} of "
          f"{len(expressions)} probes disagree.\n")
    for divergence in divergences[: args.show]:
        print(f"  {bold}{divergence.input}{reset}")
        side = divergence.before
        print(f"    before: {green}{side.value if side.ok else side.error}{reset}")
        side = divergence.after
        print(f"    after:  {red}{side.value if side.ok else side.error}{reset}")
        if divergence.kind == "message":
            print(f"    {dim}(only the error wording differs){reset}")
        print()
    remaining = len(divergences) - args.show
    if remaining > 0:
        print(f"  {dim}...and {remaining} more{reset}\n")
    print(f"{dim}{elapsed:.1f}s, ${spend:.4f}{reset}")
    return 1


def verify_diff(args: argparse.Namespace) -> int:
    """Review a change that already exists, rather than inventing one."""
    bold, dim, red, green, yellow, reset = _palette(
        not args.no_color and sys.stdout.isatty())
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"{red}not a git repository: {repo}{reset}", file=sys.stderr)
        return 2

    try:
        review = review_mod.plan(repo, args.base, args.head, max_targets=args.max_functions)
    except ValueError as exc:
        print(f"{red}{exc}{reset}", file=sys.stderr)
        return 2

    print(f"{bold}{repo.name}{reset}  {args.base}..{args.head}  "
          f"{dim}({review.base[:8]}..{review.head[:8]}){reset}")
    if not review.targets:
        print(f"\n{yellow}nothing to verify — no changed lines sit inside a "
              f"function we can probe{reset}")
        for path, why in review.skipped[:5]:
            print(f"  {dim}{path}: {why}{reset}")
        return 0
    print(f"{dim}{len(review.targets)} changed function(s) to check"
          + (f", {len(review.skipped)} skipped" if review.skipped else "") + f"{reset}\n")

    client = NemotronClient(cap_usd=args.cap)
    opening_spend = client.ledger.total_usd
    started = time.monotonic()

    executor = None
    if not args.local:
        ok, why = available()
        if ok:
            print(f"{dim}warming a checkpoint at {review.base[:8]}...{reset}")
            executor = SandboxExecutor()
            executor.warm_archive(tarball_at(repo, review.base), repo)
            print(f"{dim}  ready in {time.monotonic() - started:.1f}s, "
                  f"{executor.archive_bytes // 1024} KiB, {executor.install_mode}{reset}\n")
        else:
            print(f"{yellow}sandboxes unavailable ({why}); running locally{reset}\n")

    if executor is None:
        print(f"{red}verify-diff needs Sandboxes: comparing two revisions locally "
              f"would mean checking them out under you.{reset}", file=sys.stderr)
        return 2

    findings = 0
    for target in review.targets:
        print(f"{bold}{target.qualname}{reset}  {dim}{target.path}{reset}")
        head_overlay = {target.path: file_at(repo, review.head, target.path)}

        def probe(expressions, _overlay=None):
            return executor.observe(target.module, expressions, overlay=_overlay)

        expressions, _ = generate_validated(
            client, target.module, target.qualname,
            focused_module(target.base_source, target.qualname),
            probe=lambda e: probe(e),
            n=args.probes,
            subclasses=receiver_candidates(target.base_source, target.qualname),
            stateful=is_stateful(target.base_source, target.qualname),
            diff=review_mod.hunk(repo, review.base, review.head, target.path),
        )
        if not expressions:
            print(f"  {yellow}no usable probes{reset}\n")
            continue

        before = probe(expressions)
        after = probe(expressions, head_overlay)
        divergences, flaky = confirm(
            compare(before, after),
            run_before=lambda e: probe(e),
            run_after=lambda e: probe(e, head_overlay),
        )
        if flaky:
            print(f"  {dim}{len(flaky)} probe(s) disagreed once but not on repeat "
                  f"— discarded as nondeterministic{reset}")
        if not divergences:
            index = {o.input: o for o in after}
            agreed = sum(1 for b in before
                         if b.ok and index.get(b.input) and index[b.input].ok)
            print(f"  {green}preserved{reset} {dim}— {agreed}/{len(expressions)} "
                  f"probes agreed{reset}\n")
            continue

        findings += 1
        print(f"  {red}{bold}behaviour changed{reset} — {len(divergences)} of "
              f"{len(expressions)} probes disagree")
        for d in divergences[: args.show]:
            print(f"    {bold}{d.input}{reset}")
            print(f"      before: {green}{d.before.value if d.before.ok else d.before.error}{reset}")
            print(f"      after:  {red}{d.after.value if d.after.ok else d.after.error}{reset}")
        print()

    elapsed = time.monotonic() - started
    spend = client.ledger.total_usd - opening_spend
    if findings:
        print(f"{red}{bold}{findings} of {len(review.targets)} changed functions "
              f"behave differently.{reset}")
    else:
        print(f"{green}{bold}No behaviour change detected{reset} across "
              f"{len(review.targets)} changed functions.")
    print(f"{dim}{elapsed:.1f}s, ${spend:.4f}{reset}")
    return 1 if findings else 0


def doctor(_args: argparse.Namespace) -> int:
    from .config import have_nebius, nebius_project_id

    ok, why = available()
    print(f"  credentials   {'ok' if have_nebius() else 'NEBIUS_API_KEY missing'}")
    print(f"  project id    {'ok' if nebius_project_id() else 'NEBIUS_PROJECT_ID missing'}")
    print(f"  sandboxes     {('ok — ' + why) if ok else why}")
    print()
    print(NemotronClient().ledger.summary())
    return 0 if ok and have_nebius() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mutiny",
        description="Check whether a rewrite preserved a function's behaviour.")
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="rewrite a function, then verify the rewrite")
    v.add_argument("repo", help="path to the repository")
    v.add_argument("function", help="function to rewrite, e.g. Version.next_version")
    v.add_argument("--local", action="store_true",
                   help="run probes locally instead of in Sandboxes")
    v.add_argument("--python", default=sys.executable,
                   help="interpreter for local runs (default: this one)")
    v.add_argument("--model", default="nvidia/nemotron-3-super-120b-a12b")
    v.add_argument("--probes", type=int, default=30)
    v.add_argument("--show", type=int, default=3, help="divergences to print")
    v.add_argument("--cap", type=float, default=5.0, help="spend cap, USD")
    v.add_argument("--no-color", action="store_true")
    v.set_defaults(func=verify)

    r = sub.add_parser("verify-diff",
                       help="verify a change that already exists (branch, PR, commit)")
    r.add_argument("repo", help="path to the repository")
    r.add_argument("--base", default="main", help="revision to compare against")
    r.add_argument("--head", default="HEAD", help="revision under review")
    r.add_argument("--max-functions", type=int, default=10)
    r.add_argument("--probes", type=int, default=30)
    r.add_argument("--show", type=int, default=2)
    r.add_argument("--cap", type=float, default=5.0, help="spend cap, USD")
    r.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--no-color", action="store_true")
    r.set_defaults(func=verify_diff)

    d = sub.add_parser("doctor", help="check credentials, models and sandbox access")
    d.set_defaults(func=doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
