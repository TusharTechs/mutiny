

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
