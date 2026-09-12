"""Attacking the test suite instead of the code is the worst failure mode here:
it is silent, and it manufactures survivors that mean nothing."""
import pytest

from mutiny.diff import _is_source, enclosing_functions

SOURCE = [
    "src/pkg/core.py", "pkg/core.py", "core.py",
    "src/pkg/contest.py", "src/latest/thing.py",
]
NOT_SOURCE = [
    "tests/__init__.py",        # the bug: no leading slash to match "/tests/"
    "tests/helpers.py",
    "test/util.py",
    "src/pkg/tests/fixture.py",
    "conftest.py",
    "tests/conftest.py",
    "src/pkg/test_core.py",
    "src/pkg/core_test.py",
    "README.md",
    "noxfile.txt",
]


@pytest.mark.parametrize("path", SOURCE)
def test_source_files_are_attackable(path):
    assert _is_source(path) is True


@pytest.mark.parametrize("path", NOT_SOURCE)
def test_test_files_are_never_attacked(path):
    assert _is_source(path) is False


def test_enclosing_functions_finds_innermost_first():
    src = """
class Cache:
    def __init__(self, maxsize):
        def helper(x):
            return x + 1
        self.maxsize = helper(maxsize)
"""
    assert enclosing_functions(src, (5,))[0] == "Cache.__init__.helper"
    assert "Cache.__init__" in enclosing_functions(src, (6,))


def test_enclosing_functions_survives_unparseable_source():
    assert enclosing_functions("def f(:\n", (1,)) == []
