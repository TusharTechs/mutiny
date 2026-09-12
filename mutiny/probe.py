"""Source for a pytest plugin that records *why* each test failed.

Gate 2 rule 2 requires that a proof test fail on the mutant by assertion, not by
import error, collection crash or timeout. Pytest's textual output does not make
that distinction reliably, so we inject a plugin that reads `call.excinfo`
directly. The plugin is written to a temp directory and loaded via PYTHONPATH so
it works inside a target repo's own interpreter without installing MUTINY there.
"""
from __future__ import annotations

PLUGIN_SOURCE = '''
import json
import os
import pytest

_EXC = {}
_STATUS = {}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    nodeid = report.nodeid
    if call.excinfo is not None:
        _EXC[(nodeid, report.when)] = call.excinfo.type.__name__
    if report.when == "call":
        _STATUS[nodeid] = report.outcome
    elif report.outcome == "failed":
        # setup/teardown failure -> the test never really ran
        _STATUS.setdefault(nodeid, "error:" + report.when)


def pytest_collectreport(report):
    if report.failed:
        _STATUS[report.nodeid or "<collection>"] = "error:collect"
        longrepr = str(getattr(report, "longrepr", ""))
        first = longrepr.strip().splitlines()[-1] if longrepr.strip() else ""
        _EXC[(report.nodeid or "<collection>", "collect")] = first[:200] or "CollectError"


def pytest_sessionfinish(session, exitstatus):
    out = {}
    for nodeid, status in _STATUS.items():
        exc = (
            _EXC.get((nodeid, "call"))
            or _EXC.get((nodeid, "setup"))
            or _EXC.get((nodeid, "collect"))
        )
        out[nodeid] = {"status": status, "exc_type": exc}
    path = os.environ.get("MUTINY_PROBE_OUT")
    if path:
        with open(path, "w") as fh:
            json.dump({"exitstatus": int(exitstatus), "tests": out}, fh)
'''
