"""A click is not a submission, and six fields is not an application.

The record that prompted this:

    Fields answered   6
    Evidence          the page still shows 'required field' after submitting
    Status            submitted

An Ashby application with six of its fields answered bounced on validation,
and the row was written as sent -- which spends the posting for good,
because applications dedupe by `job_id` and nothing retries a job already
marked submitted. The DOM had said which fields were required and what each
one held since the harvester was written; nothing read them together.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from applier.apply.readiness import looks_submitted, unfilled_required

FORM = "https://jobs.example.com/co/abc/application"


@dataclass
class F:
    selector: str
    type: str = "text"
    label: str = ""
    required: bool = False
    value: str = ""

    @property
    def question(self) -> str:
        return self.label


# --------------------------------------------------------------------------- #
# before the click
# --------------------------------------------------------------------------- #
def test_an_empty_required_field_is_a_gap():
    gaps = unfilled_required([F("#a", required=True, label="Phone", value=""),
                              F("#b", required=True, label="Name", value="Ali")])
    assert [g.label for g in gaps] == ["Phone"]


def test_an_optional_field_left_empty_is_not_a_gap():
    assert unfilled_required([F("#a", required=False, value="")]) == []


@pytest.mark.parametrize("placeholder", [
    "", " ", "Select...", "select one", "Please select", "--", "N/A", "none"])
def test_a_select_still_on_its_prompt_is_not_an_answer(placeholder):
    """A required dropdown sitting on "Select..." reads as filled by value
    and is the single most common way a form bounces."""
    assert unfilled_required([F("#s", "select", "Work auth", True, placeholder)])


def test_a_file_input_is_never_counted_as_unfilled():
    """A page cannot read back a local path, so an uploaded resume still
    reports an empty value. Counting it would halt every application."""
    assert unfilled_required([F("#cv", "file", "Resume", True, "")]) == []


def test_a_real_answer_is_not_a_placeholder():
    assert unfilled_required([F("#s", "select", "Country", True, "Elbonia")]) == []


# --------------------------------------------------------------------------- #
# after the click
# --------------------------------------------------------------------------- #
def test_a_form_that_came_back_with_errors_was_not_submitted():
    """The exact case from the record."""
    ok, why = looks_submitted(
        form_url=FORM, final_url=FORM,
        page_text="Phone number is a required field. Please correct the errors.",
        gaps_before=3, gaps_after=3)
    assert not ok
    assert "required field" in why


def test_navigating_to_a_confirmation_counts_as_submitted():
    ok, why = looks_submitted(
        form_url=FORM, final_url=FORM + "/thanks",
        page_text="Thank you for applying.", gaps_before=0, gaps_after=0)
    assert ok and "moved" in why


def test_a_footer_saying_required_field_does_not_undo_a_submission():
    """Confirmation pages routinely carry "* indicates a required field".

    Reading that as a bounce would mark a sent application unsent, and the
    next run would apply to the same job again.
    """
    ok, _ = looks_submitted(
        form_url=FORM, final_url=FORM,
        page_text="Thank you. * indicates a required field",
        gaps_before=4, gaps_after=0)
    assert ok


def test_an_unreadable_page_counts_as_submitted():
    """Ambiguity resolves the safe way: the employer may have the form."""
    ok, _ = looks_submitted(form_url=FORM, final_url="", page_text="",
                            gaps_before=2, gaps_after=0)
    assert ok


def test_the_same_page_with_gaps_still_open_was_not_submitted():
    ok, why = looks_submitted(form_url=FORM, final_url=FORM, page_text="Apply now",
                              gaps_before=2, gaps_after=2)
    assert not ok and "same address" in why


def test_gaps_that_cleared_are_not_a_bounce():
    """Fewer gaps after than before means the page changed, not refused."""
    ok, _ = looks_submitted(form_url=FORM, final_url=FORM,
                            page_text="is required", gaps_before=5, gaps_after=1)
    assert ok


# --------------------------------------------------------------------------- #
# the pipeline wiring
# --------------------------------------------------------------------------- #
def test_the_gate_runs_before_the_click_and_the_check_after():
    """Ordering, read off the source: the cost of getting it wrong is a
    posting spent on an application nobody received."""
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    gate = src.index("readiness.unfilled_required")
    click = src.index("clicked = sess.click(selector)")
    check = src.index("readiness.looks_submitted")
    assert gate < click < check, "gate must precede the click, check must follow"


def test_a_bounced_submit_does_not_mark_the_row_submitted():
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    after = src[src.index("readiness.looks_submitted"):]
    body = after[:after.index("_record_submission")]
    assert "status='needs_input'" in body, (
        "a form that bounced must leave the posting re-appliable")


# --------------------------------------------------------------------------- #
# asking the browser, not just the harvest
# --------------------------------------------------------------------------- #
# The harvest is second-hand: it sees the fields this program managed to
# read, under its own visibility rules. `checkValidity()` is the computation
# the browser runs when a submit is pressed -- every constraint at once,
# over fields the harvest skipped, and over constraints it never models,
# like an email that is present and malformed.
class FakePage:
    def __init__(self, result):
        self._result = result

    def evaluate(self, _js):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_a_browser_that_refuses_the_form_blocks_the_submit():
    from applier.apply.readiness import ask_browser

    v = ask_browser(FakePage({
        "forms": 1, "blocked": True, "flagged": [],
        "invalid": [{"label": "Phone number", "why": "required, and empty"}]}))
    assert v.blocked and not v
    assert v.reasons == ["Phone number: required, and empty"]


def test_a_framework_error_counts_even_when_the_browser_is_happy():
    """React validation does not use the constraint API: it marks the field
    and writes the complaint beside it. Those fields are valid as far as
    checkValidity is concerned and are why a form bounces anyway."""
    from applier.apply.readiness import ask_browser

    v = ask_browser(FakePage({"forms": 1, "blocked": False, "invalid": [],
                              "flagged": ["Work authorization"]}))
    assert v.blocked
    assert "Work authorization" in v.reasons[0]


def test_a_valid_form_is_not_blocked():
    from applier.apply.readiness import ask_browser

    v = ask_browser(FakePage({"forms": 1, "blocked": False,
                              "invalid": [], "flagged": []}))
    assert not v.blocked and v and v.reasons == []


def test_a_page_that_cannot_be_asked_does_not_block():
    """Mid-navigation, detached, no form at all. This is one input of two,
    and neither may turn an unreadable page into a refusal to submit."""
    from applier.apply.readiness import ask_browser

    assert not ask_browser(FakePage(RuntimeError("context destroyed"))).blocked
    assert not ask_browser(FakePage(None)).blocked


def test_the_reasons_name_the_field_not_the_symptom():
    """"7 required field(s) still empty" says a form bounced. "Phone number:
    required, and empty" says what to do about it."""
    from applier.apply.readiness import ask_browser

    v = ask_browser(FakePage({
        "forms": 1, "blocked": True, "flagged": [],
        "invalid": [{"label": "Email", "why": "not a valid email"},
                    {"label": "Start date", "why": "out of range"}]}))
    assert v.reasons == ["Email: not a valid email",
                         "Start date: out of range"]


def test_both_sources_are_consulted_before_a_submit():
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    gate = src[src.index("gaps = readiness.unfilled_required"):
               src.index("clicked = sess.click(selector)")]
    assert "readiness.ask_browser" in gate, (
        "a required field the harvester cannot see is invisible to the "
        "harvest check; the browser is the other half of the answer")
    assert "gaps or verdict.blocked" in gate, "either source must be able to stop it"


# --------------------------------------------------------------------------- #
# taking a posting back
# --------------------------------------------------------------------------- #
def test_only_a_submitted_row_can_be_reclaimed(tmp_path, monkeypatch):
    from applier.db import Database, now
    from applier.web import api

    db = Database(tmp_path / "t.db")
    db.run("INSERT INTO jobs(fingerprint,weak_key,source,company,title,url,"
           "first_seen_at,last_seen_at) VALUES('f','w','t','Northwind','Intern',"
           "'https://example.com/1',?,?)", (now(), now()))
    db.run("INSERT INTO applications(job_id,started_at,status) "
           "VALUES(1,?,'needs_input')", (now(),))
    monkeypatch.setattr(api, "get_db", lambda: db)

    with pytest.raises(api.ApiError, match="nothing to take back"):
        api.reclaim_application({}, app_id=1)


def test_reclaiming_puts_the_posting_back_in_circulation(tmp_path, monkeypatch):
    """A row saying submitted removes the posting for good, because
    applications dedupe by job_id."""
    from applier.db import Database, now
    from applier.web import api

    db = Database(tmp_path / "t.db")
    db.run("INSERT INTO jobs(fingerprint,weak_key,source,company,title,url,"
           "first_seen_at,last_seen_at) VALUES('f','w','t','Northwind','Intern',"
           "'https://example.com/1',?,?)", (now(), now()))
    db.run("INSERT INTO applications(job_id,started_at,submitted_at,status,"
           "evidence) VALUES(1,?,?,'submitted','clicked')", (now(), now()))
    monkeypatch.setattr(api, "get_db", lambda: db)

    out = api.reclaim_application({"reason": "never arrived"}, app_id=1)
    assert out["ok"] and out["company"] == "Northwind"
    row = db.one("SELECT * FROM applications WHERE id=1")
    assert row["status"] == "needs_input"
    assert row["submitted_at"] is None
    assert row["evidence"] is None
    assert "never-sent" in row["failure_reason"]


def test_nothing_automatic_can_reclaim():
    """The one thing that walks a submission back stays a thing a person
    does by hand. An unattended run that could decide a submission did not
    happen is an unattended run that applies twice."""
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline)
    assert "reclaim" not in src, (
        "the pipeline must never reclaim an application on its own")
