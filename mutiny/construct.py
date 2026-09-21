"""Work out how to build the object, before trying to test it.

Four of the forty-five pull requests with real behaviour to check still produce
no verdict, and the reason is always the same: nothing the generator writes can
construct the receiver. `InstanceState` wants a session behind it, `Job` wants a
scheduler, `Retrying` wants a policy object. The generator is being asked to
solve two problems in one expression -- how do I build this, and what should I
call on it -- and it fails at the first, so the second never happens.

So solve them separately. This finds a *recipe*: a snippet that builds the
receiver and is proven to run, before any probe is written. Probes then start
from a working object rather than guessing at one.

It is a loop rather than a prompt because the useful information arrives only by
running things. Each attempt executes its candidates in the sandbox, reads what
actually failed, and decides what evidence would help -- the class definition,
then the repository's own tests -- rather than sending everything up front and
hoping. Asking for more context costs tokens, so it is asked for only when the
last attempt proved it was needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import SUPER, NemotronClient
from .scenarios import construction_examples
from .source import class_source

SUBJECT = "subject"

SYSTEM = """You write Python that builds one object, and nothing else.

You are given a class and asked for snippets that construct an instance of it.
Each snippet is one or more statements ending with an assignment to `subject`.

    cache = LRUCache(maxsize=2)
    subject = cache

Rules:
- Every name you use must be imported in the snippet or defined in it.
- Prefer the simplest construction that produces a usable object. If the
  constructor needs collaborators, build them too.
- Give several genuinely different approaches, not one approach reworded.
- No explanation, no markdown headings. Snippets separated by a blank line.
- Never use `...` or a placeholder. Every snippet must actually run."""

ASK = """Module: `{module}`
The object to build: `{owner}`

{evidence}

Write {n} different snippets that each end with `subject = <an instance of {owner}>`."""

FAILED = """None of those worked. What each one raised:

{errors}

Those are real errors from running your snippets. Write {n} different snippets
that avoid them."""


@dataclass
class Recipe:
    """A proven way to build the receiver."""
    setup: str
    owner: str
    attempts: int = 1
    evidence: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.setup.strip())


def inline_form(snippet: str) -> str:
    """The recipe as one line a probe can actually carry.

    Probes are validated line by line and imports are rejected outright, so a
    multi-line recipe beginning `from cachetools import Cache` is discarded
    before it runs -- which is how a correct recipe produced zero probes. The
    import is also unnecessary: a probe is evaluated in the target module's own
    globals, where the class it defines is already a name.
    """
    kept = [line.strip() for line in snippet.splitlines()
            if line.strip() and not line.strip().startswith(("import ", "from "))]
    return "; ".join(kept)


def _snippets(text: str) -> list[str]:
    """Split a reply into candidate snippets."""
    cleaned = re.sub(r"```(?:python)?", "", text)
    blocks = [b.strip() for b in cleaned.split("\n\n")]
    return [b for b in blocks if f"{SUBJECT}" in b and "..." not in b][:6]


def _errors(observations, limit: int = 4) -> str:
    seen, lines = set(), []
    for observation in observations:
        if observation.ok or observation.error in seen:
            continue
        seen.add(observation.error)
        lines.append(f"{observation.input.splitlines()[-1][:90]}\n    {observation.error[:160]}")
        if len(lines) >= limit:
            break
    return "\n".join(lines) or "(no error was reported)"


def find_recipe(
    client: NemotronClient,
    *,
    module: str,
    owner: str,
    source: str,
    repo,
    probe,
    qualname: str = "",
    rounds: int = 3,
    model: str = SUPER,
) -> Recipe | None:
    """Find a snippet that builds `owner`, proven by running it.

    Evidence is escalated rather than front-loaded: the class definition first,
    then the repository's own tests, and only when the previous attempt has
    shown that what we gave was not enough.
    """
    gathered: list[str] = []
    errors = ""

    for attempt in range(1, max(1, rounds) + 1):
        evidence = []
        if attempt >= 1:
            definition = class_source(source, owner)
            if definition:
                evidence.append(f"Its definition:\n```python\n{definition}\n```")
                if "class definition" not in gathered:
                    gathered.append("class definition")
        if attempt >= 3 and repo is not None:
            # Only now: somebody has already built one of these, in a test.
            examples = construction_examples(repo, qualname or owner,
                                             limit=2, module_source=source)
            if examples:
                shown = "\n\n".join(f"# {name}\n{body}" for name, body in examples)
                evidence.append(
                    f"Tests in this repository that build it:\n```python\n{shown}\n```")
                if "repository tests" not in gathered:
                    gathered.append("repository tests")

        prompt = ASK.format(module=module, owner=owner, n=4,
                            evidence="\n\n".join(evidence) or "(no definition found)")
        if errors:
            prompt += "\n\n" + FAILED.format(errors=errors, n=4)

        try:
            text, _ = client.complete(
                [{"role": "system", "content": SYSTEM},
                 {"role": "user", "content": prompt}],
                model=model, max_tokens=4000, temperature=0.4,
                tag=f"construct:{owner}:{attempt}",
            )
        except Exception:  # noqa: BLE001 - a recipe is an improvement, not a need
            return None

        candidates = _snippets(text)
        if not candidates:
            continue

        # The proof is execution: a snippet that builds the object and whose
        # trailing expression is that object.
        observations = probe([f"{c}\n{SUBJECT}" for c in candidates])
        working = [c for c, o in zip(candidates, observations) if o.ok]

        # An opaque repr is fine for a receiver: it only has to exist.
        for candidate in working:
            inline = inline_form(candidate)
            if not inline:
                continue
            # The one-line form is what a probe will carry, so prove that runs
            # rather than assuming it follows from the multi-line one.
            check = probe([f"{inline}; {SUBJECT}"])
            if check and check[0].ok:
                return Recipe(setup=inline, owner=owner, attempts=attempt,
                              evidence=list(gathered))
        if not working:
            errors = _errors(observations)

    return None
