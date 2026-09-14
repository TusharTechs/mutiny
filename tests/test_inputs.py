

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

    `awaitable` was added to generate_validated and to the call it makes, but
    not to generate itself. Every test passed — none of them calls generate —
    and the live site failed on the first pull request with a TypeError.
    """
    import inspect

    from mutiny.inputs import generate, generate_validated

    forwarded = set(inspect.signature(generate).parameters)
    for name in ("hint", "model", "subclasses", "diff", "stateful", "awaitable"):
        assert name in forwarded, f"generate() cannot accept {name!r}"

    outer = set(inspect.signature(generate_validated).parameters)
    assert {"stateful", "awaitable", "diff", "subclasses"} <= outer
