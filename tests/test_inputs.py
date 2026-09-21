

def test_a_coroutine_function_is_recognised():
    """A bare call to an async def returns a coroutine and runs none of the body.

    Probes are single expressions, so without an await they are identical before
    and after any change. tenacity's AsyncRetrying._run_wait burned two rounds of
    generation producing nothing before this was noticed.
    """
    from mutiny.inputs import is_async

    src = """
class Retrying:
    async def _run_wait(self, state):
        return await self.wait(state)

    def sync_wait(self, state):
        return 0.0
"""
    assert is_async(src, "Retrying._run_wait") is True
    assert is_async(src, "Retrying.sync_wait") is False


def test_every_generator_argument_reaches_the_prompt_builder():
    """generate_validated forwards its arguments to generate; they must exist.

    This has now broken twice, identically: `awaitable`, then `recipe`, each
    added to generate_validated and to the call it makes but not to generate
    itself. No test calls generate, so the suite stayed green and the failure
    landed on a live run.

    The first version of this test listed the arguments by hand, which is how it
    missed the second one. It now reads the call out of the source, so a new
    argument is covered the moment it is written.
    """
    import ast
    import inspect

    from mutiny.inputs import generate, generate_validated

    tree = ast.parse(inspect.getsource(generate_validated))
    forwarded = {
        keyword.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "generate"
        for keyword in node.keywords
        if keyword.arg
    }
    assert forwarded, "expected generate_validated to call generate with keywords"

    accepted = set(inspect.signature(generate).parameters)
    missing = forwarded - accepted
    assert not missing, f"generate() cannot accept {sorted(missing)}"
