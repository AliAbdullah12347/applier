"""The check that runs while someone is still watching.

`doctor` answers "is it installed". Preflight answers "will leaving this
running for six hours waste a day's application quota", which is a different
question and worth the minutes it costs: it renders a real PDF, makes a real
model call and starts a real browser, because each of those is a thing that
looks fine until it is used.

The distinction that matters is blocking versus advisory. A missing TeX
distribution means every application goes out with an untailored resume, which
is worse than not running. Two gigabytes of free disk instead of twenty is
worth saying and not worth stopping for.
"""

from __future__ import annotations

import pytest

from applier.preflight import Check, Report, _safe


def test_a_blocking_failure_stops_the_run():
    r = Report([Check("a", True), Check("b", False, blocking=True)])
    assert r.ok is False
    assert [c.name for c in r.blocking_failures] == ["b"]


def test_an_advisory_failure_does_not():
    r = Report([Check("a", True), Check("b", False, blocking=False)])
    assert r.ok is True
    assert [c.name for c in r.warnings] == ["b"]


def test_everything_passing_is_ready():
    assert Report([Check("a", True), Check("b", True)]).ok is True


def test_a_check_that_raises_becomes_a_failed_check_not_a_crash():
    """Preflight exists to prevent a wasted run; crashing is a wasted run."""
    def boom() -> Check:
        raise RuntimeError("the network is on fire")

    c = _safe(boom, "exploding", True)
    assert c.ok is False and c.blocking is True
    assert "network is on fire" in c.detail


def test_the_report_serialises_for_the_gui():
    r = Report([Check("a", False, "why", blocking=True, fix="do this")])
    d = r.as_dict()
    assert d["ok"] is False
    assert d["blocking"] == ["a"]
    assert d["checks"][0]["fix"] == "do this"


@pytest.mark.parametrize("state,expected", [
    ((True, False), "ok"),
    ((False, False), "warn"),
    ((False, True), "BLOCKED"),
])
def test_states_read_the_way_they_are_printed(state, expected):
    ok, blocking = state
    assert Check("x", ok, blocking=blocking).state == expected


def test_an_unattended_run_refuses_to_start_on_a_blocking_failure():
    """The reason this module exists at all."""
    import inspect

    from applier import cli
    src = inspect.getsource(cli.run)
    assert "run_preflight" in src, "`applier run` no longer checks before starting"
    assert "skip_preflight" in src, "there is no deliberate override"
    assert "not report.ok" in src


def test_the_quick_path_skips_the_expensive_checks():
    import inspect

    from applier import preflight
    src = inspect.getsource(preflight.run_preflight)
    for expensive in ("_check_renderer", "_check_model", "_check_browser"):
        assert expensive in src
    assert "if deep:" in src, "the expensive checks are not behind the deep flag"


def test_the_autonomy_check_says_whether_it_will_submit():
    """Before an unattended run, that is the only line that really matters."""
    import inspect

    from applier import preflight
    src = inspect.getsource(preflight._check_autonomy)
    assert "submits automatically" in src and "never submits" in src
