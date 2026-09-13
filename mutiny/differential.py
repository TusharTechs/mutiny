"""Run two versions of the same code on the same inputs and compare.

MUTINY's proof-test generation asked Nemotron to produce the *evidence* — a test
that mechanically demonstrates a specific bug — and it managed that 13% of the
time at both 120B and 550B. Differential execution inverts the burden: the model
produces only *inputs*, and execution produces the evidence.

That distinction is the whole design. An assertion has to be correct to be
useful. An input does not: if it is nonsense, both versions reject it the same
way and it simply contributes nothing. Bad inputs are wasted, never wrong.

Inputs are call expressions evaluated in the module's own namespace, which means
one uniform mechanism covers module functions, constructors, class methods and
chained calls on instances.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DRIVER = r'''
import ast, importlib, json, os, random, re, sys

sys.path.insert(0, os.getcwd())

# When the editable install fails -- sqlalchemy's needs a C toolchain and does
# not get one -- the package is still sitting in the tree, just not on the path.
# src-layout projects keep it one level down, so look there too.
for _layout in ("src", "lib"):
    _dir = os.path.join(os.getcwd(), _layout)
    if os.path.isdir(_dir):
        sys.path.insert(1, _dir)

# Anything drawing on `random` differs between two runs for reasons the caller
# cannot control. cachetools' RRCache evicts a random entry, and comparing two
# unseeded runs of it reports a behaviour change on every probe.
random.seed(0)

# repr() falls back to the object's memory address, which differs on every
# process. Left alone it makes any object without a custom __repr__ diverge by
# construction -- three of six apparent detections in one run were nothing but
# differing addresses.
_ADDRESS = re.compile(r"(?: at | @ )(?:0x[0-9a-fA-F]+|\d{6,})")
_OPAQUE = re.compile(r"^<[\w.]+ object>$")


def canonical(value, depth=0):
    """A repr that does not vary with things the caller cannot observe.

    Sets and dicts have no defined iteration order, so their repr reorders
    between processes and makes identical values look different -- two apparent
    detections were frozensets holding exactly the same tags. Membership is the
    behaviour; ordering is not.
    """
    if depth > 6:
        return "..."
    if isinstance(value, (set, frozenset)):
        inner = sorted(canonical(v, depth + 1) for v in value)
        return ("frozenset({" if isinstance(value, frozenset) else "{") + \
               ", ".join(inner) + ("})" if isinstance(value, frozenset) else "}")
    if isinstance(value, dict):
        items = sorted((canonical(k, depth + 1), canonical(v, depth + 1))
                       for k, v in value.items())
        return "{" + ", ".join(f"{k}: {v}" for k, v in items) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(canonical(v, depth + 1) for v in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(canonical(v, depth + 1) for v in value)
        return f"({inner},)" if len(value) == 1 else f"({inner})"
    return _ADDRESS.sub("", repr(value))


def describe(value):
    try:
        text = canonical(value)[:600]
    except Exception:
        text = _ADDRESS.sub("", repr(value)[:600])
    return text, bool(_OPAQUE.match(text.strip()))

module_name, out_path = sys.argv[1], sys.argv[2]
exprs = json.load(open(sys.argv[3]))


def _bootstrap():
    """Some frameworks cannot be imported until they have been configured.

    Importing django.db.models.base raises ImproperlyConfigured before a single
    probe runs, and no input the generator writes can get past it -- the barrier
    is at import time, not call time. A framework that ships a documented
    bootstrap is given it. Anything else is left alone: this is deliberately a
    short list of known bootstraps, not a guess at what a project might need.
    """
    try:
        import django
        from django.conf import settings
    except ImportError:
        return
    if settings.configured:
        return
    try:
        settings.configure(
            DEBUG=True, USE_TZ=True, SECRET_KEY="mutiny", ALLOWED_HOSTS=["*"],
            # Compiled translation catalogues are not source and are not
            # uploaded, so leave gettext out of the picture entirely.
            USE_I18N=False,
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3",
                                   "NAME": ":memory:"}},
            INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth"],
        )
        django.setup()
    except Exception:
        pass


_bootstrap()
mod = importlib.import_module(module_name)
base = dict(vars(mod))

def run(snippet, ns):
    """Statements, then a final expression whose value is the observation.

    Stateful behaviour needs a sequence — filling a cache and then replacing an
    entry with a larger one cannot be written as one expression — so a snippet
    is executed and its trailing expression evaluated.
    """
    tree = ast.parse(snippet, mode="exec")
    if not tree.body:
        return None
    if isinstance(tree.body[-1], ast.Expr):
        head = ast.Module(body=tree.body[:-1], type_ignores=[])
        exec(compile(head, "<snippet>", "exec"), ns)
        tail = ast.Expression(body=tree.body[-1].value)
        return eval(compile(tail, "<snippet>", "eval"), ns)
    exec(compile(tree, "<snippet>", "exec"), ns)
    return None

results = []
for expr in exprs:
    rec = {"input": expr}
    try:
        value = run(expr, dict(base))
        rec["ok"] = True
        rec["type"] = type(value).__name__
        try:
            rec["value"], rec["opaque"] = describe(value)
        except Exception as exc:
            rec["value"] = f"<unreprable {type(value).__name__}: {exc}>"
            rec["opaque"] = True
    except Exception as exc:
        rec["ok"] = False
        rec["type"] = type(exc).__name__
        rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
        rec["message"] = _ADDRESS.sub("", " ".join(str(exc).split()))[:300]
    except BaseException as exc:
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__} (fatal)"
    results.append(rec)

if out_path == "-":
    sys.stdout.write("\x00MUTINY\x00" + json.dumps(results))
else:
    json.dump(results, open(out_path, "w"))
'''


@dataclass(frozen=True)
class Observation:
    input: str
    ok: bool
    value: str | None = None
    error: str | None = None
    opaque: bool = False
    type: str | None = None
    message: str | None = None

    @property
    def outcome(self) -> str:
        """What a caller actually sees.

        The type is part of it: a function whose return type changed has changed
        behaviour even when the two reprs happen to read alike.

        Exception messages are included too. They were excluded at first on the
        theory that rewording is noise — but four of ten missed commits were
        *about* error behaviour ("reject invalid tags", "normalize invalid
        specifier errors", "aggregate validation errors"), so discarding the
        message made those undetectable by construction. Message-only
        differences are reported as their own, weaker class rather than
        suppressed; see `kind`.
        """
        if self.ok:
            return f"value:{self.type}:{self.value}"
        return f"raised:{self.type}:{self.message}"

    @property
    def coarse(self) -> str:
        """Outcome ignoring an exception's wording — what changed structurally."""
        return f"value:{self.type}:{self.value}" if self.ok else f"raised:{self.type}"


@dataclass(frozen=True)
class Divergence:
    input: str
    before: Observation
    after: Observation

    @property
    def kind(self) -> str:
        """`behaviour` when the value or failure changed; `message` when only an
        error's wording did. Both are real, but they are not equally strong."""
        return "behaviour" if self.before.coarse != self.after.coarse else "message"

    def __str__(self) -> str:
        def side(o: Observation) -> str:
            return o.value if o.ok else f"raises {o.error}"
        tag = "" if self.kind == "behaviour" else "  [message only]"
        return (f"{self.input}{tag}\n    before: {side(self.before)}"
                f"\n    after:  {side(self.after)}")


def observe(
    repo: Path,
    module: str,
    expressions: list[str],
    python_exe: str | None = None,
    timeout: int = 300,
    baseline: bool = True,
) -> list[Observation]:
    """Evaluate each expression in `module`'s namespace, recording what happened.

    `baseline` says whether this is the unmodified side. Only there does a batch
    in which everything fails on an undefined name mean we are misconfigured. On
    the modified side it usually means the opposite — the change broke the module
    outright, which is the most emphatic finding available, and raising on it
    threw away a true detection.
    """
    python_exe = python_exe or sys.executable
    with tempfile.TemporaryDirectory(prefix="mutiny-diff-") as tmp:
        tmpd = Path(tmp)
        (tmpd / "driver.py").write_text(DRIVER, encoding="utf-8")
        (tmpd / "exprs.json").write_text(json.dumps(expressions), encoding="utf-8")
        out = tmpd / "out.json"
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Fixed so string hashing, and therefore set iteration, is stable across
        # the two runs being compared.
        env["PYTHONHASHSEED"] = "0"
        proc = subprocess.run(
            [python_exe, str(tmpd / "driver.py"), module, str(out), str(tmpd / "exprs.json")],
            cwd=repo, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if not out.exists():
            raise RuntimeError(
                f"driver produced nothing: {(proc.stdout + proc.stderr).strip()[-300:]}"
            )
        raw = json.loads(out.read_text())
    observations = [
        Observation(r["input"], r["ok"], r.get("value"), r.get("error"),
                    r.get("opaque", False), r.get("type"), r.get("message"))
        for r in raw
    ]

    # On the unmodified side, every expression failing on an undefined name means
    # we are looking in the wrong module rather than that the inputs were bad.
    # Reported as zero usable inputs that is indistinguishable from a hard
    # problem, and it silently cost ten of nineteen cases in one run.
    if baseline and observations and all(not o.ok for o in observations):
        kinds = {(o.error or "").split(":", 1)[0] for o in observations}
        if kinds <= {"NameError", "AttributeError", "ImportError", "ModuleNotFoundError"}:
            raise LookupError(
                f"every expression failed with {'/'.join(sorted(kinds))} in module "
                f"{module!r} — the symbols are probably defined in a submodule. "
                f"First: {observations[0].input!r} -> {observations[0].error}"
            )

    return observations


def compare(before: list[Observation], after: list[Observation]) -> list[Divergence]:
    """Inputs on which the two versions behave differently."""
    index = {o.input: o for o in after}
    out = []
    for b in before:
        a = index.get(b.input)
        if a is None:
            continue
        # `<Thing object>` tells us nothing about behaviour: two entirely
        # different objects share that repr, so it can neither witness a
        # divergence nor evidence agreement.
        if b.opaque or a.opaque:
            continue
        if a.outcome != b.outcome:
            out.append(Divergence(b.input, b, a))
    return out


def agreement(before: list[Observation], after: list[Observation]) -> tuple[int, int, int]:
    """(inputs compared, inputs both versions accepted, divergences)."""
    index = {o.input: o for o in after}
    compared = usable = diverged = 0
    for b in before:
        a = index.get(b.input)
        if a is None:
            continue
        compared += 1
        if b.opaque or a.opaque:
            continue
        usable += b.ok and a.ok
        diverged += a.outcome != b.outcome
    return compared, usable, diverged


def confirm(
    divergences: list[Divergence],
    run_before,
    run_after,
    rounds: int = 2,
) -> tuple[list[Divergence], list[Divergence]]:
    """Re-run the divergent probes; keep only those that disagree every time.

    Seeding `random` handles the obvious case, but nondeterminism has many
    sources — clocks, iteration order, address-derived hashing, the network — and
    a comparison of two single runs cannot tell a real change from a coin toss.
    Confirming costs one extra pair of runs over the divergent probes alone,
    which is usually a handful, and it is the difference between a tool people
    trust and one they mute.

    Returns (confirmed, flaky).
    """
    if not divergences:
        return [], []
    inputs = [d.input for d in divergences]

    # First: is the probe even deterministic within one version? ShortUUID.uuid()
    # returns a fresh random value on every call, so it differs across versions
    # every time and confirmation alone endorses it — the divergence reproduces
    # perfectly because the probe is random, not because behaviour changed.
    # A probe that cannot agree with itself cannot testify about anything.
    first = {o.input: o for o in run_before(inputs)}
    second = {o.input: o for o in run_before(inputs)}
    stable = {
        name for name in inputs
        if (a := first.get(name)) is not None
        and (b := second.get(name)) is not None
        and a.outcome == b.outcome
    }
    still: set[str] = set(stable)
    for _ in range(max(1, rounds)):
        before = {o.input: o for o in run_before(inputs)}
        after = {o.input: o for o in run_after(inputs)}
        for name in list(still):
            b, a = before.get(name), after.get(name)
            if b is None or a is None or b.opaque or a.opaque or a.outcome == b.outcome:
                still.discard(name)
        if not still:
            break
    confirmed = [d for d in divergences if d.input in still]
    flaky = [d for d in divergences if d.input not in still]
    return confirmed, flaky
