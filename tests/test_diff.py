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


def test_enclosing_functions_stops_at_the_outermost_callable():
    """A nested helper is not a probeable target; the function holding it is.

    This used to return "Cache.__init__.helper" — the innermost match, on the
    reasoning that it is the most specific. It is, and it is also unreachable:
    nothing outside __init__ can call it, function_span cannot resolve the name,
    and the target was dropped as "ambiguous or absent in one revision". Twelve
    pull requests in the corpus failed this way.
    """
    src = """
class Cache:
    def __init__(self, maxsize):
        def helper(x):
            return x + 1
        self.maxsize = helper(maxsize)
"""
    assert enclosing_functions(src, (5,))[0] == "Cache.__init__"
    assert "Cache.__init__" in enclosing_functions(src, (6,))


def test_a_changed_decorator_belongs_to_its_function():
    """ast puts lineno on the def, leaving a decorator attached to nothing."""
    src = """
import functools


@functools.lru_cache(maxsize=None)
def expensive(n):
    return n * 2
"""
    assert enclosing_functions(src, (5,)) == ["expensive"]


def test_changes_outside_a_function_reach_whatever_reads_them():
    """A module-level constant is not inside a function and still has callers."""
    from mutiny.diff import dependent_functions

    src = """
import re

VERSION = re.compile(r"[0-9]+")


def parse(text):
    match = VERSION.match(text)
    return VERSION.findall(text) if match else []


def unrelated(x):
    return x + 1
"""
    assert dependent_functions(src, (4,)) == ["parse"]


def test_a_changed_class_attribute_reaches_the_methods_that_use_it():
    from mutiny.diff import dependent_functions

    src = """
class Locale:
    TIMEFRAMES = {"week": "a week", "weeks": "{0} weeks"}

    def humanize(self, unit, count):
        return self.TIMEFRAMES[unit].format(count)

    def name(self):
        return "en"
"""
    assert dependent_functions(src, (3,)) == ["Locale.humanize"]


def test_packaging_and_documentation_are_not_code_under_test():
    assert _is_source("setup.py") is False
    assert _is_source("docs/conf.py") is False
    assert _is_source("noxfile.py") is False
    assert _is_source("src/pkg/core.py") is True


def test_enclosing_functions_survives_unparseable_source():
    assert enclosing_functions("def f(:\n", (1,)) == []
