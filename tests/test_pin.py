"""Pinning a witness as a test the repository keeps."""
import subprocess
import sys

from mutiny.pin import pin


DIVERGENCES = [{"input": "_widen(2)", "before": "'4'", "after": "'6'"}]


def build(tmp_path, returns):
    """A throwaway module to pin against, so the test does not depend on a branch."""
    (tmp_path / "pinned_subject.py").write_text(
        f"def _widen(n):\n    return str(n * {returns})\n")
    return tmp_path


def run_generated(tmp_path, text):
    path = tmp_path / "test_generated.py"
    path.write_text(text)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           str(path)], capture_output=True, text=True, cwd=str(tmp_path))


def test_the_generated_test_passes_against_the_side_it_pins(tmp_path):
    """A generated test that does not run is worse than none at all."""
    build(tmp_path, 3)  # _widen(2) == '6', the `after` value
    text, count = pin(DIVERGENCES, module="pinned_subject", qualname="_widen",
                      side="after")
    assert count == 1
    result = run_generated(tmp_path, text)
    assert result.returncode == 0, result.stdout


def test_the_generated_test_fails_against_the_other_side(tmp_path):
    """Otherwise it pins nothing and would pass whatever the code did."""
    build(tmp_path, 3)  # still '6', so pinning `before` ('4') must fail
    text, _ = pin(DIVERGENCES, module="pinned_subject", qualname="_widen",
                  side="before")
    assert run_generated(tmp_path, text).returncode != 0


def test_underscored_names_are_imported_explicitly():
    """`import *` skips them, and an internal helper is the usual target.

    The first generated test failed on NameError for `_is_source`.
    """
    text, _ = pin([{"input": "_is_source('a.py')", "before": "True", "after": "False"}],
                  module="mutiny.diff", qualname="_is_source")
    assert "from mutiny.diff import _is_source" in text
    assert "import *" not in text


def test_an_exception_is_pinned_as_a_raises():
    text, count = pin(
        [{"input": "f(None)", "before": "0.0",
          "after": "TypeError: 'NoneType' object is not callable"}],
        module="pkg.mod", qualname="f", side="after")
    assert count == 1
    assert "with pytest.raises(TypeError):" in text


def test_a_value_we_cannot_reproduce_faithfully_is_refused():
    """Canonicalised containers were sorted and addresses stripped for comparison.

    Pinning either would write a test that fails for the wrong reason.
    """
    text, count = pin(
        [{"input": "thing()", "before": "<pkg.Thing 0xADDR>", "after": "<pkg.Thing 0xADDR>"},
         {"input": "tags()", "before": "{'a', 'b'}", "after": "{'b', 'c'}"}],
        module="pkg.mod", qualname="thing", side="after")
    assert count == 0
    assert "No witness could be pinned" in text


def test_statements_before_the_expression_are_kept():
    text, _ = pin(
        [{"input": "c = Cache(2); c['a'] = 1; len(c)", "before": "1", "after": "0"}],
        module="pkg.cache", qualname="Cache.__setitem__", side="after")
    assert "c = Cache(2)" in text and "c['a'] = 1" in text
    assert "assert repr(len(c)) == '0'" in text
