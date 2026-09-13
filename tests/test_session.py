"""Session-level orchestration: target selection and its fallbacks."""
from pathlib import Path



def test_repository_walks_to_the_next_candidate_when_probes_fail(monkeypatch):
    """A target that yields no probe must hand over, not end the run.

    The first version of this checked the wrong key on the event dict, so the
    fallback silently never fired and django reported nothing.
    """
    from mutiny import session

    monkeypatch.setattr(session, "_candidate_functions", lambda *a, **k: ["first", "second"])
    monkeypatch.setattr(session, "available", lambda: (False, "test"))

    attempted = []

    def fake_verify(repo, qualname, **kwargs):
        attempted.append(qualname)
        if qualname == "first":
            yield {"type": "no_probes", "function": qualname}
        else:
            yield {"type": "result", "function": qualname, "divergences": []}

    monkeypatch.setattr(session, "verify_function", fake_verify)

    events = list(session._verify_repository(
        Path("."), "owner/repo", probes=4, forks=2, cap=1.0))

    assert attempted == ["first", "second"]
    assert not any(e["type"] == "no_probes" for e in events)
    assert [e for e in events if e["type"] == "result"][0]["function"] == "second"


def test_changed_between_matches_what_git_would_report(tmp_path):
    """The tree diff has to agree with the git diff it replaces."""
    import subprocess

    from mutiny.diff import changed_between, changed_lines

    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, env=env,
                       capture_output=True)

    (repo / "pkg" / "m.py").write_text("def f(x):\n    return x + 1\n\n\ndef g():\n    return 2\n")
    git("init", "-q"); git("add", "-A"); git("commit", "-qm", "base")
    (repo / "pkg" / "m.py").write_text(
        "def f(x):\n    if x < 0:\n        return 0\n    return x + 1\n\n\ndef g():\n    return 2\n")
    git("add", "-A"); git("commit", "-qm", "head")

    by_git = {c.path: c.lines for c in changed_lines(repo, "HEAD", "HEAD^")}

    before, after = tmp_path / "before", tmp_path / "after"
    (before / "pkg").mkdir(parents=True); (after / "pkg").mkdir(parents=True)
    (before / "pkg" / "m.py").write_text("def f(x):\n    return x + 1\n\n\ndef g():\n    return 2\n")
    (after / "pkg" / "m.py").write_text(
        "def f(x):\n    if x < 0:\n        return 0\n    return x + 1\n\n\ndef g():\n    return 2\n")
    by_tree = {c.path: c.lines for c in changed_between(before, after)}

    assert by_git == by_tree == {"pkg/m.py": (2, 3)}


def test_overlay_applies_every_changed_file_not_just_the_target(tmp_path):
    """A change is one state. Applying half of it invents one that never existed.

    rich#3180 changed `divide_line` and the `chop_cells` it calls. Overlaying
    only the file holding the target ran the new caller against the old helper
    and reported a confident TypeError that no released version of that library
    could raise. The witness was real; the state it came from was not.
    """
    from mutiny.review import plan_between

    before, after = tmp_path / "before", tmp_path / "after"
    for tree, helper_arg in ((before, "def helper(text):"), (after, "def helper(text, *, upper=False):")):
        (tree / "pkg").mkdir(parents=True)
        (tree / "pkg" / "__init__.py").write_text("")
        (tree / "pkg" / "helper.py").write_text(
            f"{helper_arg}\n    return text\n")
    (before / "pkg" / "caller.py").write_text(
        "from .helper import helper\n\n\ndef call(text):\n    return helper(text)\n")
    (after / "pkg" / "caller.py").write_text(
        "from .helper import helper\n\n\ndef call(text):\n    return helper(text, upper=True)\n")

    review = plan_between(before, after, "b", "h")
    assert set(review.paths) == {"pkg/caller.py", "pkg/helper.py"}, (
        "both changed files must be in the overlay set, or the 'after' run is "
        f"a state that never existed: {review.paths}")
