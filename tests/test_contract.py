"""Documentation is evidence only if it says what we claim it says."""
from mutiny.contract import Contract, Source, _verbatim, query_for


PAGE = Source(
    title="API reference",
    url="https://example.readthedocs.io/api.html",
    content="Retrying accepts a wait argument. If wait is not supplied, no delay "
            "is applied between attempts and the call is retried immediately.",
)


def test_a_quote_that_is_on_the_page_is_accepted():
    quote = "If wait is not supplied, no delay is applied between attempts"
    assert _verbatim(quote, [PAGE]) is PAGE


def test_a_quote_that_is_not_on_the_page_is_refused():
    """A model paraphrasing retrieved text and attaching a link is worse than
    silence: it dresses an invention as a citation."""
    assert _verbatim("Retrying raises TypeError when wait is None", [PAGE]) is None


def test_whitespace_and_case_do_not_defeat_the_check():
    quote = "IF WAIT IS NOT SUPPLIED,   no delay is applied\nbetween attempts"
    assert _verbatim(quote, [PAGE]) is PAGE


def test_a_changelog_fragment_is_too_thin_to_count():
    """'Fixed humanize month limits.' is 28 characters and promises nothing."""
    page = Source("Release notes", "https://x/releases.html",
                  "1.2.0 Fixed humanize month limits. Other changes follow.")
    assert _verbatim("Fixed humanize month limits.", [page]) is None


def test_the_verdict_that_matters_is_the_documented_old_behaviour():
    assert Contract("old", "q", "u", "t").contradicts is True
    assert Contract("new", "q", "u", "t").contradicts is False


def test_the_query_names_the_package_and_the_function():
    query = query_for("jd/tenacity#679", "BaseRetrying._run_wait", "wait is called")
    assert "tenacity" in query and "_run_wait" in query


def test_a_rewrite_that_describes_itself_not_at_all_is_still_checked():
    """An agent's rewrite comes with no description, which is not the same as
    a description saying nothing changed — and is the case where "did the
    project promise the old behaviour?" is the only external evidence there is.
    """
    import inspect

    from mutiny import session

    source = inspect.getsource(session._probe_and_compare)
    assert "described is not True and contract_mod.available()" in source, (
        "a change with no description at all must not skip the check")


def test_a_similarly_named_library_is_not_this_project():
    """Searching for python-semver returns python-semanticversion: a different
    library, similar name, similar API. Quoting its documentation as this
    project's promise is a wrong claim that a citation makes look verified.
    """
    from mutiny.contract import belongs_to

    assert belongs_to("https://python-semver.readthedocs.io/en/latest/usage.html",
                      "python-semver/python-semver")
    assert belongs_to("https://pypi.org/project/semver", "python-semver/python-semver")
    assert belongs_to("https://github.com/python-semver/python-semver",
                      "python-semver/python-semver")
    assert not belongs_to(
        "https://python-semanticversion.readthedocs.io/en/latest/reference.html",
        "python-semver/python-semver")


def test_the_name_check_is_a_floor_and_not_a_guarantee():
    """Go's semver package shares the name and would pass this test.

    Worth recording honestly rather than implying the check is airtight: it
    removes the common case of a similarly-named neighbour, and a same-named
    library in another ecosystem still gets through. The verbatim-quote check
    is what stops that becoming an invented claim.
    """
    from mutiny.contract import belongs_to

    assert belongs_to("https://pkg.go.dev/golang.org/x/mod/semver",
                      "python-semver/python-semver") is True


def test_the_check_survives_a_slug_with_no_owner():
    from mutiny.contract import belongs_to

    assert belongs_to("https://arrow.readthedocs.io/en/stable/", "arrow")
