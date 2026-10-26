"""Which question a field is asking.

Everything downstream depends on this. The field's label is what the model is
shown, what the legal resolver matches against, and what ends up in the audit
trail as the question that was answered. Get it wrong and the system fills the
field confidently with the answer to a different question.

That is not a hypothetical. Label discovery used to walk up the ancestors
calling `node.querySelector('label, ...')`, which searches that ancestor's
whole subtree -- so once the walk reached the `<form>`, the first match was
the first label on the entire form. A graduation-date field with no label of
its own was told it was called "First name" and filled with one, which went
into a real-looking application and would have gone to a recruiter.

The field this happens to is always the awkward one nobody labelled properly,
which is exactly the field worth getting right.

Run against a real browser, because this is DOM behaviour and nothing else
decides it.
"""

from __future__ import annotations

import pytest

from applier.apply.universal import HARVEST_JS

pytest.importorskip("playwright.sync_api")

# The shapes real application forms use, including the two that broke.
PAGE = """<!doctype html><html><body>
<form>
  <label for="first_name">First name *</label>
  <input type="text" id="first_name" name="first_name">

  <label for="last_name">Last name *</label>
  <input type="text" id="last_name" name="last_name">

  <!-- No label element at all: the question is a preceding paragraph. -->
  <p>Expected graduation (YYYY-MM)</p>
  <input type="text" name="grad" id="grad">

  <!-- aria-label, which some ATS vendors use instead -->
  <input type="text" id="portfolio" aria-label="Portfolio URL">

  <!-- the question wraps the input -->
  <label>Why do you want to work here?
    <textarea id="why"></textarea>
  </label>

  <!-- a field group with the question in a div above it -->
  <div><div>Are you legally authorised to work?</div>
       <select id="auth"><option>Yes</option><option>No</option></select></div>
</form></body></html>"""


@pytest.fixture(scope="module")
def labels():
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(PAGE)
                fields = page.evaluate(HARVEST_JS)
            finally:
                browser.close()
    except Exception as exc:                            # noqa: BLE001
        pytest.skip(f"no usable browser: {exc}")
    return {f["selector"]: (f.get("label") or "") for f in fields}


def test_a_field_with_its_own_label_gets_it(labels):
    assert labels.get("#first_name", "").startswith("First name")
    assert labels.get("#last_name", "").startswith("Last name")


def test_an_unlabelled_field_takes_the_question_above_it(labels):
    """The bug: this one was told it was called "First name"."""
    got = labels.get("#grad", "")
    assert "graduation" in got.lower(), (
        f"the graduation field is labelled {got!r}; anything filled here goes "
        f"into the wrong box on a real application")
    assert "first name" not in got.lower()


def test_no_two_fields_claim_the_same_question(labels):
    """Two fields with one label means at least one of them is wrong."""
    interesting = {sel: lab for sel, lab in labels.items() if lab}
    duplicates = {
        lab for lab in interesting.values()
        if list(interesting.values()).count(lab) > 1
    }
    assert not duplicates, f"these labels are claimed by more than one field: {duplicates}"


def test_aria_label_is_used(labels):
    assert labels.get("#portfolio", "") == "Portfolio URL"


def test_a_wrapping_label_is_used(labels):
    assert "why do you want" in labels.get("#why", "").lower()


def test_a_question_in_a_preceding_div_is_used(labels):
    assert "authorised" in labels.get("#auth", "").lower()


def test_every_field_got_some_label(labels):
    unlabelled = [sel for sel, lab in labels.items() if not lab.strip()]
    assert not unlabelled, f"fields with no question at all: {unlabelled}"
