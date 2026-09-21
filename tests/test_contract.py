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
