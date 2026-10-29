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
