"""`mutiny` — did that change preserve behaviour?

Two commands over one engine:

    mutiny verify <repo> <function>        rewrite it with Nemotron, then check
    mutiny verify-diff <repo> --base ...   check a change you already have

Both render the event stream from `mutiny.session`, which the web interface
renders too. The loop lives there; this file only decides how it looks in a
terminal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .session import verify_diff as run_diff
from .session import verify_function as run_function
from .session import verify_url as run_url

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m")


def _emphasise(before: str, after: str, c: dict) -> tuple[str, str]:
    """Dim what the two values share, so the part that differs stands out.

    Two reprs differing in one field are a spot-the-difference puzzle:

        before  Version(major=1, minor=0, patch=0, prerelease=None, build='alpha')
        after   Version(major=1, minor=0, patch=0, prerelease=None, build='alpha.0')

    Trimming the shared head and tail leaves what actually changed, which is the
    only reason to print the pair together.
    """
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1

    if head + tail < 4:  # too little in common for trimming to help
        return (f"{c['green']}{before}{c['reset']}", f"{c['red']}{after}{c['reset']}")

    def paint(text: str, colour: str) -> str:
        middle = text[head:len(text) - tail] if tail else text[head:]
        return (f"{c['dim']}{text[:head]}{c['reset']}"
                f"{colour}{c['bold']}{middle}{c['reset']}"
                f"{c['dim']}{text[len(text) - tail:] if tail else ''}{c['reset']}")

    return paint(before, c["green"]), paint(after, c["red"])


def _wrap(text: str, width: int) -> list[str]:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


class Render:
    """Turns the event stream into something readable in a terminal."""

    def __init__(self, colour: bool, show: int, interactive: bool = True):
        keys = ("bold", "dim", "red", "green", "yellow", "cyan", "reset")
        values = (BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET) if colour else ("",) * 7
        self.c = dict(zip(keys, values))
        self.show = show
        # Carriage returns overwrite in a terminal and smear everywhere else —
        # piped into a file or a CI log every intermediate state is kept.
        self.interactive = interactive
        self.forks: dict[str, dict[int, str]] = {}
        self.findings = 0

    def __call__(self, event: dict) -> None:
        handler = getattr(self, f"_{event['type']}", None)
        if handler is not None:
            handler(event)

    def _target(self, e: dict) -> None:
        c = self.c
        print(f"{c['bold']}{e['repo']}{c['reset']}  {e['path']}  "
              f"{c['bold']}{e['function']}{c['reset']}  "
              f"{c['dim']}({e['module']}){c['reset']}\n")

    def _review(self, e: dict) -> None:
        c = self.c
        print(f"{c['bold']}{e['repo']}{c['reset']}  {e['base']}..{e['head']}")
        if not e["targets"]:
            print(f"\n{c['yellow']}nothing to verify — no changed lines sit inside "
                  f"a function we can probe{c['reset']}")
            for skip in e["skipped"][:5]:
                print(f"  {c['dim']}{skip['path']}: {skip['why']}{c['reset']}")
            return
        extra = f", {len(e['skipped'])} skipped" if e["skipped"] else ""
        print(f"{c['dim']}{len(e['targets'])} changed function(s) to check{extra}"
              f"{c['reset']}\n")

    def _status(self, e: dict) -> None:
        print(f"{self.c['dim']}{e['text']}...{self.c['reset']}")

    def _checkpoint(self, e: dict) -> None:
        c = self.c
        print(f"{c['dim']}  ready in {e['seconds']}s, {e['kib']} KiB, "
              f"{e['mode']}{c['reset']}")

    def _diff(self, e: dict) -> None:
        c = self.c
        for line in e["lines"]:
            colour = (c["green"] if line.startswith("+")
                      else c["red"] if line.startswith("-") else c["dim"])
            print(f"    {colour}{line}{c['reset']}")

    def _probes(self, e: dict) -> None:
        print(f"{self.c['dim']}  {e['count']} probes{self.c['reset']}")

    def _no_probes(self, e: dict) -> None:
        print(f"  {self.c['yellow']}no usable probes{self.c['reset']}\n")

    def _fetched(self, e: dict) -> None:
        c = self.c
        kind = "pull request" if e["pull_request"] else "repository"
        print(f"{c['dim']}  {kind} {c['reset']}{c['bold']}{e['slug']}{c['reset']}\n")

    def _function(self, e: dict) -> None:
        c = self.c
        print(f"{c['bold']}{e['name']}{c['reset']}  {c['dim']}{e['path']}{c['reset']}")

    def _fork(self, e: dict) -> None:
        side = self.forks.setdefault(e["side"], {})
        side[e["index"]] = e["state"]
        if e["state"] == "running" and len(side) < e["total"]:
            return
        glyphs = "".join(
            {"running": "·", "done": "•", "failed": "x"}.get(
                side.get(i, "running"), "·")
            for i in range(e["total"]))
        done = sum(1 for s in side.values() if s == "done")
        if not self.interactive and done != e["total"]:
            return
        end = "\n" if done == e["total"] else "\r"
        print(f"  {self.c['dim']}{e['side']:<6}{self.c['reset']} "
              f"{self.c['cyan']}{glyphs}{self.c['reset']} "
              f"{self.c['dim']}{done}/{e['total']} forks{self.c['reset']}",
              end=end, flush=True)
        if done == e["total"]:
            self.forks[e["side"]] = {}

    def _flaky(self, e: dict) -> None:
        print(f"  {self.c['dim']}{e['count']} probe(s) disagreed once but not on "
              f"repeat — discarded as nondeterministic{self.c['reset']}")

    def _result(self, e: dict) -> None:
        c = self.c
        if not e["divergences"]:
            print(f"  {c['green']}preserved{c['reset']} {c['dim']}— {e['agreed']}/"
                  f"{e['probes']} probes ran on both versions and agreed{c['reset']}\n")
            return
        self.findings += 1
        described = e.get("described")
        if described is False:
            # The case the whole tool exists for: the change said it was not
            # going to do this.
            print(f"  {c['red']}{c['bold']}NOT DESCRIBED BY THIS CHANGE{c['reset']} — "
                  f"{len(e['divergences'])} of {e['probes']} probes disagree\n")
        elif described is True:
            print(f"  {c['green']}as described{c['reset']} — behaviour changed on "
                  f"{len(e['divergences'])} of {e['probes']} probes, and the change "
                  f"says so\n")
        else:
            print(f"  {c['red']}{c['bold']}behaviour changed{c['reset']} — "
                  f"{len(e['divergences'])} of {e['probes']} probes disagree\n")
        if e.get("summary"):
            for line in _wrap(e["summary"], 76):
                print(f"    {c['bold']}{line}{c['reset']}")
            print()
        for d in e["divergences"][: self.show]:
            print(f"    {c['dim']}{d['input']}{c['reset']}")
            before, after = _emphasise(str(d["before"]), str(d["after"]), c)
            print(f"      before  {before}")
            print(f"      after   {after}")
            if d["kind"] == "message":
                print(f"      {c['dim']}(only the error wording differs){c['reset']}")
        remaining = len(e["divergences"]) - self.show
        if remaining > 0:
            print(f"    {c['dim']}...and {remaining} more{c['reset']}")
        print()

    def _verdict(self, e: dict) -> None:
        c = self.c
        if e["functions"] and e["findings"]:
            print(f"{c['red']}{c['bold']}{e['findings']} of {e['functions']} changed "
                  f"function(s) behave differently.{c['reset']}")
        elif e["functions"]:
            print(f"{c['green']}{c['bold']}No behaviour change detected{c['reset']} "
                  f"across {e['functions']} function(s).")
        print(f"{c['dim']}{e['seconds']}s, ${e['cost']:.4f}{c['reset']}")

    def _error(self, e: dict) -> None:
        print(f"{self.c['red']}{e['message']}{self.c['reset']}", file=sys.stderr)


def _render(events, colour: bool, show: int) -> int:
    render = Render(colour, show, interactive=sys.stdout.isatty())
    failed = False
    for event in events:
        render(event)
        failed |= event["type"] == "error"
    return 2 if failed else (1 if render.findings else 0)


def verify(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not repo.is_dir():
        print(f"no such repository: {repo}", file=sys.stderr)
        return 2
    return _render(
        run_function(repo, args.function, model=args.model, probes=args.probes,
                     forks=args.forks, cap=args.cap, use_sandbox=not args.local,
                     python=args.python),
        not args.no_color and sys.stdout.isatty(), args.show)


def verify_diff(args: argparse.Namespace) -> int:
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        print(f"not a git repository: {repo}", file=sys.stderr)
        return 2
    return _render(
        run_diff(repo, args.base, args.head, probes=args.probes, forks=args.forks,
                 cap=args.cap, max_functions=args.max_functions),
        not args.no_color and sys.stdout.isatty(), args.show)


def verify_url(args: argparse.Namespace) -> int:
    return _render(
        run_url(args.url, probes=args.probes, forks=args.forks, cap=args.cap,
                max_functions=args.max_functions),
        not args.no_color and sys.stdout.isatty(), args.show)


def doctor(_args: argparse.Namespace) -> int:
    from .config import have_nebius, nebius_project_id
    from .models import NemotronClient
    from .sandbox import available

    ok, why = available()
    print(f"  credentials   {'ok' if have_nebius() else 'NEBIUS_API_KEY missing'}")
    print(f"  project id    {'ok' if nebius_project_id() else 'NEBIUS_PROJECT_ID missing'}")
    print(f"  sandboxes     {('ok — ' + why) if ok else why}")
    print()
    print(NemotronClient().ledger.summary())
    return 0 if ok and have_nebius() else 1


def review_pr(args: argparse.Namespace) -> int:
    """Check the pull request this run is about, and comment only if it matters.

    Exit status is 0 whether or not a difference was found. A behaviour
    difference is information for a reviewer to weigh, not a verdict, and a bot
    that fails builds on its own judgement is switched off within a week.
    """
    from . import ci
    from .report import collect, render

    if args.url:
        url = args.url
        context = None
    else:
        try:
            context = ci.context()
        except ci.CIError as exc:
            print(f"mutiny: {exc}")
            return 1
        url = context.url

    print(f"mutiny: checking {url}")
    report = collect(run_url(url, probes=args.probes, forks=args.forks,
                             cap=args.cap, max_functions=args.max_functions))
    body = render(report)
    speak = report.worth_saying(args.comment_on)

    print(f"\n{body}\n")
    ci.summary(body)

    if context is None:
        print("mutiny: not running in a pull request, so nothing was posted")
        return 0
    if not context.token:
        print("mutiny: no GITHUB_TOKEN, so nothing was posted")
        return 0
    try:
        print(f"mutiny: {ci.publish(context, body, speak=speak)}")
    except ci.CIError as exc:
        # Failing the build because a comment could not be posted would be the
        # tool getting in the way of the work it is meant to support.
        print(f"mutiny: could not post the comment — {exc}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mutiny",
        description="Check whether a change preserved a function's behaviour.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--probes", type=int, default=30)
        p.add_argument("--forks", type=int, default=8)
        p.add_argument("--show", type=int, default=3, help="witnesses to print")
        p.add_argument("--cap", type=float, default=5.0, help="spend cap, USD")
        p.add_argument("--no-color", action="store_true")

    v = sub.add_parser("verify", help="rewrite a function, then verify the rewrite")
    v.add_argument("repo")
    v.add_argument("function", help="e.g. Version.next_version")
    v.add_argument("--local", action="store_true",
                   help="run probes on this machine instead of in Sandboxes")
    v.add_argument("--python", default=sys.executable)
    v.add_argument("--model", default="nvidia/nemotron-3-super-120b-a12b")
    common(v)
    v.set_defaults(func=verify)

    r = sub.add_parser("verify-diff",
                       help="verify a change that already exists (branch, PR, commit)")
    r.add_argument("repo")
    r.add_argument("--base", default="main", help="revision to compare against")
    r.add_argument("--head", default="HEAD", help="revision under review")
    r.add_argument("--max-functions", type=int, default=10)
    common(r)
    r.set_defaults(func=verify_diff)

    u = sub.add_parser("verify-url",
                       help="verify a GitHub repository or pull request by URL")
    u.add_argument("url", help="https://github.com/owner/repo[/pull/N]")
    u.add_argument("--max-functions", type=int, default=6)
    common(u)
    u.set_defaults(func=verify_url)

    c = sub.add_parser("review-pr",
                       help="check the pull request this CI run is about")
    c.add_argument("--url", default="",
                   help="check this pull request instead of the one in the environment")
    c.add_argument("--comment-on", default="undescribed",
                   choices=("undescribed", "any", "never"),
                   help="when to post: only undescribed changes (default), "
                        "every result, or never")
    c.add_argument("--probes", type=int, default=20)
    c.add_argument("--forks", type=int, default=8)
    c.add_argument("--max-functions", type=int, default=6)
    c.add_argument("--cap", type=float, default=1.0)
    c.set_defaults(func=review_pr)

    d = sub.add_parser("doctor", help="check credentials, models and sandbox access")
    d.set_defaults(func=doctor)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
