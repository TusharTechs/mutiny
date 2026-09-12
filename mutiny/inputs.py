"""Ask Nemotron for call expressions that exercise a function hard.

This is the model's whole job in the differential design, and it is a much
easier job than writing a proof test.

The change under review is shown to the generator. Withholding it makes for a
purer experiment — detection is then genuinely blind — but it is the wrong
product. When the job is to verify a change someone just made, the change is
precisely what the inputs should target, and a generator shown only the working
code has no reason to produce the malformed inputs that a validation fix is
about. Four commits were missed exactly that way.

Super is the generator, which is not the obvious choice. Measured on the same
prompt asking for 45 snippets: Nano spends 26,000 characters reasoning before it
writes anything and frequently exhausts its allowance first, producing nothing;
Lightning returns a single usable line; Super does no reasoning at all on this
task and answers directly in a third of the time. Task shape decides the model,
not parameter count -- Super was the wrong choice for writing proof tests, where
the work genuinely needed deliberation. A bad input costs one execution on each
side and contributes nothing; a wrong assertion, by contrast, produces a false
finding. Getting it right most of the time is not required — coverage is.
"""
from __future__ import annotations

import ast
import re

from .models import SUPER, NemotronClient

SYSTEM = """You produce Python call expressions that exercise a function across
its interesting behaviour.

Each snippet runs inside the module's own namespace, so every public name in
that module is already available to you — classes, constants, helpers. No
imports, no function or class definitions, no loops.

A snippet is usually a single expression. When the behaviour is stateful it may
instead be a short sequence of statements separated by semicolons, ending in an
expression whose value is what gets compared. Use that form whenever the
interesting behaviour only appears after several operations — filling a
container and then replacing an entry, for instance:

    c = Cache(2, getsizeof=len); c["a"] = "x"; c["b"] = "yy"; c["b"] = "zzzz"; (dict(c), c.currsize)

Put the whole snippet on one line. Compare-worthy values only: return a tuple of
the things that matter rather than an object whose repr hides them.

Aim for the edges, because that is where two versions of a function differ:
boundary values, empty and single-element collections, zero and negative
numbers, None where it is permitted, values that differ only in case or
whitespace, the largest and smallest plausible values, strings that nearly
parse, and ordinary well-formed values for contrast.

Expressions that raise are fine and useful — a rejection is behaviour too. Do
not attempt anything that touches the network, the filesystem, the clock or
randomness, and never write an infinite loop.

Return only a fenced python block containing one snippet per line."""

USER = """Module `{module}`, the function under test is `{qualname}`:

```python
{source}
```

{extra}{receivers}{stateful}{change}Write {n} distinct snippets that reach `{qualname}`. If it is a
method, construct the receiver inline as part of the snippet."""

STATEFUL = """`{owner}` accumulates state, so a single call reveals almost
nothing about it. Write scenarios rather than calls. A scenario constructs the
object with a definite capacity and sizing rule, drives it through several
operations — including past its capacity — and then observes everything that
matters at once:

    c = LRUCache(maxsize=3, getsizeof=len); c["a"]="x"; c["b"]="yy"; c["b"]="zzzz"; (sorted(c.items()), c.currsize, len(c))

Vary deliberately across your snippets: the capacity, the sizes of the values,
whether a key is new or being replaced, whether the replacement is larger or
smaller, the order of access before the operation, and how far past capacity the
sequence goes. The boundary where something is evicted is where two versions
differ.

Always end with a tuple of the observable state — contents, size, length — never
the object itself, whose repr hides exactly what changed.

"""

CHANGE = """This is the change under review:

```diff
{diff}
```

Concentrate on inputs that reach the lines it touches and would show their
effect. If the change concerns validation or error handling, that means
malformed and out-of-range inputs, not only well-formed ones — the interesting
behaviour is on the path being changed. Do not assume the change is correct.

"""

RECEIVERS = """`{owner}` may be a base class whose behaviour is only observable
through a concrete subclass. These are available in this module, and you should
use them rather than `{owner}` itself unless you are deliberately testing the
base: {names}.

"""

BANNED_NAMES = {"open", "exec", "eval", "compile", "__import__", "input",
                "exit", "quit", "breakpoint", "globals", "locals", "vars"}

# Reject reaching *into* an object, not calling a dunder on it. Blocking every
# attribute beginning with "__" also blocked `Cache().__setitem__(0, 0)`, which
# is precisely the expression needed to exercise a __setitem__ under test — and
# it silently discarded every input for two of the six validation cases.
INTROSPECTION_ATTRS = {
    "__code__", "__globals__", "__dict__", "__class__", "__closure__",
    "__func__", "__wrapped__", "__subclasses__", "__bases__", "__mro__",
    "__builtins__", "__loader__", "__spec__", "__reduce__", "__getattribute__",
}

_FENCE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def _valid(expr: str) -> bool:
    """A single expression, no statements, no obvious escape hatches."""
    expr = expr.strip()
    if not expr or expr.startswith("#"):
        return False
    try:
        tree = ast.parse(expr, mode="exec")
    except SyntaxError:
        return False
    if not tree.body or not isinstance(tree.body[-1], ast.Expr):
        return False  # nothing to observe
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef, ast.While,
                             ast.Global, ast.Nonlocal)):
            return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            return False
        if isinstance(node, ast.Attribute) and node.attr in INTROSPECTION_ATTRS:
            return False
    return True


def _is_memo(attr: str) -> bool:
    """Lazily filled caches are not accumulating state.

    packaging's Version is immutable, but every comparison method writes
    self._key_cache on first use. Counting that made an immutable value type look
    like a container."""
    lowered = attr.lower()
    return lowered.endswith(("_cache", "_memo", "_cached")) or "cache" in lowered


MUTATORS = {"__setitem__", "__delitem__", "__iadd__", "append", "add", "update",
            "pop", "popitem", "clear", "insert", "remove", "extend", "push",
            "expire", "evict", "put", "set"}


def is_stateful(full_source: str, qualname: str) -> bool:
    """Does this method belong to a type whose behaviour accumulates?

    A single call cannot reveal it. cachetools' over-eviction only appears after
    filling a cache past its capacity and then growing an existing entry, and
    that gap has now hidden three separate changes from us.
    """
    if "." not in qualname:
        return False
    owner = qualname.rsplit(".", 2)[-2]
    try:
        tree = ast.parse(full_source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == owner):
            continue
        names = {c.name for c in node.body
                 if isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if names & MUTATORS:
            return True
        # Or some method other than a constructor assigns to self. Assignment in
        # __init__ is initialisation, not accumulation — counting it made every
        # class with a constructor look stateful, semver's immutable Version
        # included.
        for method in node.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if method.name in {"__init__", "__new__", "__setstate__", "__post_init__"}:
                continue
            for child in ast.walk(method):
                if (isinstance(child, ast.Attribute)
                        and isinstance(child.ctx, ast.Store)
                        and isinstance(child.value, ast.Name)
                        and child.value.id == "self"
                        and not _is_memo(child.attr)):
                    return True
    return False


def receiver_candidates(full_source: str, qualname: str) -> list[str]:
    """Concrete subclasses that can stand in for the class owning `qualname`.

    A method defined on an abstract base is often unobservable through the base
    itself. cachetools' Cache.__setitem__ over-evicted when growing an entry,
    but Cache.popitem raises NotImplementedError, so nothing is ever evicted and
    the bug cannot appear — it only shows through LRUCache or LFUCache. Naming
    the subclasses turns an undetectable change into a one-line reproduction.
    """
    if "." not in qualname:
        return []
    owner = qualname.rsplit(".", 2)[-2]
    try:
        tree = ast.parse(full_source)
    except SyntaxError:
        return []

    parents: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            parents[node.name] = [
                b.id if isinstance(b, ast.Name) else getattr(b, "attr", "")
                for b in node.bases
            ]

    found, frontier = [], {owner}
    while frontier:
        nxt = set()
        for name, bases in parents.items():
            if name not in found and name != owner and frontier & set(bases):
                found.append(name)
                nxt.add(name)
        frontier = nxt
    return found


def extract(text: str) -> list[str]:
    blocks = _FENCE.findall(text)
    body = max(blocks, key=len) if blocks else text
    seen: dict[str, None] = {}
    for line in body.splitlines():
        line = line.strip().rstrip(",")
        if _valid(line):
            seen.setdefault(line, None)
    return list(seen)


def generate(
    client: NemotronClient,
    module: str,
    qualname: str,
    source: str,
    n: int = 40,
    hint: str = "",
    model: str = SUPER,
    max_tokens: int = 14000,
    subclasses: list[str] | None = None,
    diff: str = "",
    stateful: bool = False,
) -> list[str]:
    receivers = ""
    if subclasses:
        receivers = RECEIVERS.format(
            owner=qualname.rsplit(".", 2)[-2], names=", ".join(subclasses[:8]))
    text, _ = client.complete(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER.format(
                module=module, qualname=qualname, source=source, n=n,
                receivers=receivers,
                stateful=(STATEFUL.format(owner=qualname.rsplit(".", 2)[-2])
                          if stateful and "." in qualname else ""),
                change=CHANGE.format(diff=diff[:4000]) if diff else "",
                extra=(hint + "\n\n") if hint else "")},
        ],
        model=model, max_tokens=max_tokens, temperature=0.7,
        tag=f"inputs:{module}:{qualname}",
    )
    return extract(text)


REPAIR = """Of those {total} expressions, only {ok} could be evaluated. The rest
failed on the unmodified code, so they cannot compare anything.

The most common failures:

{errors}

{examples}Write {n} fresh expressions that actually run. Construct objects the
way the working code does — check the constructor's real signature in the source
above rather than guessing it."""


def _error_digest(observations, limit: int = 6) -> str:
    from collections import Counter

    counts: Counter[str] = Counter()
    samples: dict[str, str] = {}
    for o in observations:
        if o.ok:
            continue
        key = (o.error or "").split(":", 1)[0]
        counts[key] += 1
        samples.setdefault(key, f"{o.input}  ->  {o.error}")
    return "\n".join(f"  {n}x  {samples[k]}" for k, n in counts.most_common(limit))


def generate_validated(
    client: NemotronClient,
    module: str,
    qualname: str,
    source: str,
    probe,
    n: int = 45,
    batch: int = 15,
    rounds: int = 2,
    min_yield: float = 0.5,
    covering_tests: list[tuple[str, str]] | None = None,
    subclasses: list[str] | None = None,
    diff: str = "",
    stateful: bool = False,
    model: str = SUPER,
):
    """Generate inputs, run them against the unmodified code, and re-ask if too
    few survive.

    Checking the inputs before comparing anything costs one local execution and
    is the difference between a usable run and forty-five expressions that fail
    identically on both sides — which is indistinguishable from agreement.
    """
    hint = ""
    if covering_tests:
        joined = "\n\n".join(f"# {tid}\n{src}" for tid, src in covering_tests)
        hint = ("These existing tests already exercise this code. They show how the "
                f"objects involved are really constructed:\n\n```python\n{joined}\n```")

    # Accumulate across rounds rather than keeping only the best one. Generation
    # runs warm, so a round that yields little still usually yields something,
    # and discarding it throws away inputs that cost the same as the ones kept.
    # It also damps the run-to-run variance that makes small comparisons
    # unreadable.
    kept: dict[str, None] = {}
    usable_obs: list = []
    seen: set[str] = set()

    # Asked for 45 snippets in one reply the model sometimes runs out of room and
    # returns nothing at all — a third of cases at one point. Several smaller
    # requests cost the same in total and cannot fail whole.
    batches = max(1, -(-n // max(1, batch)))

    for attempt in range(1, rounds + 1):
        produced: list[str] = []
        for _ in range(batches):
            produced += generate(client, module, qualname, source, n=batch, hint=hint,
                                 model=model, subclasses=subclasses, diff=diff,
                                 stateful=stateful)
        exprs = [e for e in dict.fromkeys(produced) if e not in seen]
        if not exprs:
            if attempt == rounds:
                break
            continue
        seen.update(exprs)
        obs = probe(exprs)
        usable = [o for o in obs if o.ok]
        for o in usable:
            kept.setdefault(o.input, None)
        usable_obs += usable

        rate = len(usable) / len(obs) if obs else 0.0
        if rate >= min_yield or attempt == rounds:
            break
        hint = REPAIR.format(
            total=len(obs), ok=len(usable), errors=_error_digest(obs),
            examples=(hint + "\n\n") if covering_tests else "", n=n)

    return list(kept), usable_obs
