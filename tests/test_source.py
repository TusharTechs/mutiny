

def test_overload_stubs_do_not_make_the_real_function_ambiguous():
    """tenacity declares `retry` four times: three @t.overload, one real.

    Counting the stubs made the implementation unreachable, and the pull request
    that changed it reported no target at all.
    """
    import ast

    from mutiny.source import find_function

    src = '''
import typing as t


@t.overload
def retry(func: t.Callable) -> t.Callable: ...


@t.overload
def retry(*dargs, **dkw) -> t.Any: ...


def retry(*dargs, **dkw):
    return dargs
'''
    node = find_function(ast.parse(src), "retry")
    assert node is not None, "the implementation must win over its stubs"
    assert len(node.body) == 1 and not node.decorator_list


def test_a_genuinely_duplicated_function_is_still_ambiguous():
    """Ignoring overloads must not turn every redefinition into a guess."""
    import ast

    from mutiny.source import find_function

    src = '''
import sys

if sys.version_info >= (3, 11):
    def f(x):
        return x
else:
    def f(x):
        return x + 1
'''
    assert find_function(ast.parse(src), "f") is None
