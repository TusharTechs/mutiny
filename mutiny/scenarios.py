"""Where the objects come from.

A single call expression tests a function. A stateful class needs a *scenario*:
construct with a particular capacity and sizing function, fill it past that
capacity, then act. The generator can write those — it has, when asked directly
— but left to itself it reaches for `f(1, 2)` and gives up when the receiver
needs a paragraph of setup.

This is the recurring failure. It defeated `Cache.__setitem__`, every ORM
function in sqlalchemy, and a third of the pull requests in the corpus that had
real behaviour to check.

The repository almost always contains the answer already. Its own test suite is
full of exactly these sequences, written by people who know how the objects go
together, and they are sitting in the tree we have already downloaded.

Reading them statically rather than running them is deliberate: a suite that
cannot be installed still has readable tests, and mining takes milliseconds
where a coverage run takes minutes. `coverage.py` answers the sharper question —
which tests reach *this line* — and needs the suite to execute. This works when
that is not available, which is most of the time.
"""
from __future__ import annotations

import ast
from pathlib import Path

from .diff import TEST_DIRS

MAX_LINES = 26

# Methods every unittest.TestCase has. Seeing self.assertEqual in an example
# tells the model nothing it needs, and is not a reason to reject the example.
INHERITED = ("assert", "fail", "subTest", "skipTest", "addCleanup", "setUp",
             "tearDown", "id", "shortDescription", "run", "debug")
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "build", "dist", "node_modules"}


def _is_test_file(path: Path, repo: Path) -> bool:
    rel = path.relative_to(repo)
    if SKIP_DIRS & set(rel.parts):
        return False
    name = rel.name.lower()
    return (name.startswith("test_") or name.endswith("_test.py")
            or any(part.lower() in TEST_DIRS for part in rel.parts[:-1]))


def _targets(qualname: str) -> tuple[str, str | None]:
    """(the name being called, the class it hangs off if there is one)."""
    if "." in qualname:
        owner, name = qualname.rsplit(".", 1)
        return name, owner.split(".")[-1]
    return qualname, None


def _unresolved_self(node: ast.AST, bindings: set[str]) -> bool:
    """Does this test lean on a `self.X` the reader cannot resolve?

    cachetools' suite is a mixin: the tests say `self.Cache(maxsize=2)` and the
    concrete class is bound on a subclass in another file. Shown as a template
    that pattern is worse than nothing — the model copies `self.Cache` into a
    probe, every probe fails, and the measured yield went from 24 usable inputs
    down to 8. An example that cannot be followed is not an example.
    """
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Attribute):
            continue
        value = inner.value
        if not (isinstance(value, ast.Name) and value.id == "self"):
            continue
        if inner.attr.startswith(INHERITED) or inner.attr in bindings:
            continue
        return True
    return False


def _class_bindings(klass: ast.ClassDef) -> list[str]:
    """Simple `Name = value` assignments in a test class body.

    cachetools' suite is a mixin: every test says `self.Cache(maxsize=2)` and the
    concrete class is bound once, as `Cache = LRUCache`, on the subclass. Shown
    the test alone, the model copies `self.Cache` into a probe and it fails.
    Shown the binding too, it can resolve it.
    """
    out = []
    for statement in klass.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out.append(f"{target.id} = {ast.unparse(statement.value)}")
                except Exception:  # noqa: BLE001 - context is optional
                    continue
    return out[:6]


def _score(node: ast.AST, name: str, owner: str | None, concrete: set[str]) -> int:
    """How directly does this test exercise the target?

    Calling the method is worth more than naming the class, and constructing the
    class is worth more than importing it, because what we need from the example
    is the construction rather than the assertion.
    """
    calls = attributes = constructs = mentions = 0
    direct = indirect = 0
    for inner in ast.walk(node):
        if isinstance(inner, ast.Attribute) and inner.attr == name:
            attributes += 1
        elif isinstance(inner, ast.Name) and inner.id == name:
            mentions += 1
        elif isinstance(inner, ast.Call):
            func = inner.func
            if isinstance(func, ast.Name) and owner and func.id == owner:
                constructs += 1
            elif isinstance(func, ast.Attribute) and func.attr == name:
                calls += 1
            # Constructing something the module under test actually defines is
            # a template a probe can copy. Constructing self.Something is not.
            if isinstance(func, ast.Name) and func.id in concrete:
                direct += 1
            elif (isinstance(func, ast.Attribute)
                  and isinstance(func.value, ast.Name) and func.value.id == "self"):
                indirect += 1
    if owner:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id == owner:
                mentions += 1
    return (calls * 6 + constructs * 4 + attributes * 2 + mentions
            + direct * 5 - indirect * 2)


def _body_source(node: ast.AST, lines: list[str]) -> str:
    """The test as written, without its decorators, trimmed."""
    start = node.lineno - 1
    end = min(node.end_lineno or node.lineno, start + MAX_LINES)
    snippet = lines[start:end]
    while snippet and not snippet[-1].strip():
        snippet.pop()
    text = "\n".join(snippet)
    if (node.end_lineno or 0) > end:
        text += "\n    ..."
    return text


def concrete_names(source: str) -> set[str]:
    """Classes and functions a module defines, which a probe can name directly."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def construction_examples(
    repo: Path, qualname: str, limit: int = 3, module_source: str = ""
) -> list[tuple[str, str]]:
    """Tests from the repository that build the objects this target needs.

    Returns (test id, source) pairs, best first, in the shape
    `generate_validated` already accepts.
    """
    name, owner = _targets(qualname)
    concrete = concrete_names(module_source)
    found: list[tuple[int, str, str]] = []

    for path in sorted(repo.rglob("*.py")):
        if not path.is_file() or not _is_test_file(path, repo):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            continue
        if name not in text and (owner is None or owner not in text):
            continue  # cheap reject before walking
        lines = text.splitlines()

        enclosing: dict[int, ast.ClassDef] = {}
        for klass in ast.walk(tree):
            if isinstance(klass, ast.ClassDef):
                for child in klass.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        enclosing[id(child)] = klass

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            score = _score(node, name, owner, concrete)
            if score <= 0:
                continue
            # A test that takes fixtures shows the assertions but hides the
            # construction, which is the part we came for.
            args = [a.arg for a in node.args.args if a.arg != "self"]
            if args:
                # A fixture argument shows the assertions and hides the
                # construction, which is the part we came for.
                continue
            if score <= 0:
                continue

            klass = enclosing.get(id(node))
            bindings = _class_bindings(klass) if klass is not None else []
            bound = {b.split(" = ", 1)[0] for b in bindings}
            if _unresolved_self(node, bound):
                continue

            rel = path.relative_to(repo)
            snippet = _body_source(node, lines)
            if bindings:
                preamble = "\n".join(f"# {klass.name}: {b}" for b in bindings)
                snippet = f"{preamble}\n{snippet}"
            found.append((score, f"{rel}::{node.name}", snippet))

    found.sort(key=lambda item: -item[0])
    seen: set[str] = set()
    examples: list[tuple[str, str]] = []
    for _, test_id, source in found:
        body = source.split("\n", 1)[-1].strip()
        if body in seen:
            continue
        seen.add(body)
        examples.append((test_id, source))
        if len(examples) >= limit:
            break
    return examples
