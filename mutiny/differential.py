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
import importlib, json, sys

module_name, out_path = sys.argv[1], sys.argv[2]
exprs = json.load(open(sys.argv[3]))
mod = importlib.import_module(module_name)
ns = dict(vars(mod))

results = []
for expr in exprs:
    rec = {"input": expr}
    try:
        value = eval(expr, ns)
        rec["ok"] = True
        try:
            rec["value"] = repr(value)[:600]
        except Exception as exc:
            rec["value"] = f"<unreprable {type(value).__name__}: {exc}>"
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
    return [Observation(r["input"], r["ok"], r.get("value"), r.get("error")) for r in raw]


def compare(before: list[Observation], after: list[Observation]) -> list[Divergence]:
    """Inputs on which the two versions behave differently."""
    index = {o.input: o for o in after}
    out = []
    for b in before:
        a = index.get(b.input)
        if a is not None and a.outcome != b.outcome:
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
        usable += b.ok and a.ok
        diverged += a.outcome != b.outcome
    return compared, usable, diverged
