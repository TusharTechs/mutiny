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

DRIVER = '''
import ast, importlib, json, re, sys

# repr() falls back to the object's memory address, which differs on every
# process. Left alone it makes any object without a custom __repr__ diverge by
# construction -- three of six apparent detections in one run were nothing but
# differing addresses.
_ADDRESS = re.compile(r"(?: at | @ )(?:0x[0-9a-fA-F]+|\d{6,})")
_OPAQUE = re.compile(r"^<[\w.]+ object>$")


def describe(value):
    text = repr(value)[:600]
    text = _ADDRESS.sub("", text)
    return text, bool(_OPAQUE.match(text.strip()))

module_name, out_path = sys.argv[1], sys.argv[2]
exprs = json.load(open(sys.argv[3]))
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
        try:
            rec["value"], rec["opaque"] = describe(value)
        except Exception as exc:
            rec["value"] = f"<unreprable {type(value).__name__}: {exc}>"
            rec["opaque"] = True
    except Exception as exc:
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
    except BaseException as exc:
        rec["ok"] = False
        rec["error"] = f"{type(exc).__name__} (fatal)"
    results.append(rec)

json.dump(results, open(out_path, "w"))
'''


@dataclass(frozen=True)
class Observation:
    input: str
    ok: bool
    value: str | None = None
    error: str | None = None
    opaque: bool = False

    @property
    def outcome(self) -> str:
        """What a caller actually sees — a value, or a kind of failure.

        Exception *messages* are deliberately excluded: they change with
        refactoring far more often than behaviour does, and treating a reworded
        error as a divergence would bury the real ones.
        """
        if self.ok:
            return f"value:{self.value}"
        return f"raised:{(self.error or '').split(':', 1)[0]}"


@dataclass(frozen=True)
class Divergence:
    input: str
    before: Observation
    after: Observation

    def __str__(self) -> str:
        def side(o: Observation) -> str:
            return o.value if o.ok else f"raises {o.error}"
        return f"{self.input}\n    before: {side(self.before)}\n    after:  {side(self.after)}"


def observe(
    repo: Path,
    module: str,
    expressions: list[str],
    python_exe: str | None = None,
    timeout: int = 300,
) -> list[Observation]:
    """Evaluate each expression in `module`'s namespace, recording what happened."""
    python_exe = python_exe or sys.executable
    with tempfile.TemporaryDirectory(prefix="mutiny-diff-") as tmp:
        tmpd = Path(tmp)
        (tmpd / "driver.py").write_text(DRIVER, encoding="utf-8")
        (tmpd / "exprs.json").write_text(json.dumps(expressions), encoding="utf-8")
        out = tmpd / "out.json"
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
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
                    r.get("opaque", False))
        for r in raw
    ]

    # Every expression failing on an undefined name means we are looking in the
    # wrong module, not that the inputs were bad. Reported as zero usable inputs
    # this is indistinguishable from a hard problem, and it silently cost ten of
    # nineteen cases in one run. Fail loudly instead.
    if observations and all(not o.ok for o in observations):
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
