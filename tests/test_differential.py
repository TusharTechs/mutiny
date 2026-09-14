

def driver_namespace():
    """The driver is a string injected into the sandbox, so it cannot be imported.

    That also meant its canonicalisation — the most correctness-critical code in
    the project, and the source of five of the six false-finding classes on
    record — could only ever be tested end to end, through a sandbox. Executing
    the definitions directly makes them testable in milliseconds.
    """
    from mutiny.differential import DRIVER

    namespace: dict = {}
    # Everything above the argv parsing is pure definitions.
    definitions = DRIVER[: DRIVER.index("module_name, out_path")]
    exec(compile(definitions, "<driver>", "exec"), namespace)  # noqa: S102
    return namespace


def test_an_identity_written_without_the_word_at_is_still_an_identity():
    """tenacity writes <RetryCallState 140737342193440: ...> with no " at ".

    The address filter only matched the default repr shape, so a pull request
    that did nothing but add @override decorators reported eight divergences —
    every one an address that shifted because the decorators changed the
    allocation order. confirm() could not catch it: two fresh processes allocate
    identically, so the value looked stable within each version and different
    between them.
    """
    strip = driver_namespace()["_strip_addresses"]

    before = "<RetryCallState 140737342193440: attempt #0; slept for 0.0>"
    after = "<RetryCallState 140737342194608: attempt #0; slept for 0.0>"
    assert strip(before) == strip(after)
    assert strip(before).startswith("<RetryCallState 0xADDR")


def test_stripping_identities_does_not_swallow_values():
    strip = driver_namespace()["_strip_addresses"]

    assert strip("<X 140737342193440: n=1>") != strip("<X 140737342193440: n=2>")
    assert strip("Point(x=42, y=7)") == "Point(x=42, y=7)"
    assert strip("Row(id=123456, name='a')") == "Row(id=123456, name='a')"
    assert strip("<mutiny.Thing object at 0x7f9a1b2c3d4e>") == "<mutiny.Thing object>"


def test_a_low_entropy_random_probe_is_not_evidence():
    """Two samples cannot tell a coin from a constant.

    shortuuid's random(length=1) draws one character from 57. Asked twice
    whether it was deterministic it said yes, and a corpus run reported the
    resulting difference as a behaviour change.
    """
    from mutiny.differential import Observation, confirm, compare

    values = iter(["a", "a", "b", "a", "b", "a", "b", "a", "b", "a"])

    def run_before(inputs):
        return [Observation(input=i, ok=True, value=next(values), type="str")
                for i in inputs]

    def run_after(inputs):
        return [Observation(input=i, ok=True, value="z", type="str") for i in inputs]

    before = [Observation(input="roll()", ok=True, value="a", type="str")]
    after = [Observation(input="roll()", ok=True, value="z", type="str")]

    confirmed, flaky = confirm(compare(before, after), run_before, run_after)
    assert confirmed == [], "a probe that disagrees with itself cannot testify"
    assert len(flaky) == 1


def test_a_genuinely_stable_divergence_still_survives_confirmation():
    from mutiny.differential import Observation, compare, confirm

    def run_before(inputs):
        return [Observation(input=i, ok=True, value="1", type="int") for i in inputs]

    def run_after(inputs):
        return [Observation(input=i, ok=True, value="2", type="int") for i in inputs]

    before = [Observation(input="f()", ok=True, value="1", type="int")]
    after = [Observation(input="f()", ok=True, value="2", type="int")]

    confirmed, flaky = confirm(compare(before, after), run_before, run_after)
    assert len(confirmed) == 1 and flaky == []


def test_the_driver_provides_what_the_prompt_promises():
    """Probes run in the target module's globals and can only name what it imported.

    The generator is instructed to drive coroutines with asyncio.run(...). That
    was a promise the driver did not keep unless the module happened to import
    asyncio itself, so every such probe died on NameError and two full rounds of
    generation produced nothing.
    """
    from mutiny.differential import DRIVER
    from mutiny.inputs import ASYNC

    assert 'base.setdefault("asyncio"' in DRIVER
    assert "asyncio.run" in ASYNC, "the instruction and the namespace must agree"
