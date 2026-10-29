"""The last step: finding the submit button and pressing it.

Everything before this is covered -- the form is reached, the fields are
filled, the resume uploads, the audit trail records it. What remained unproven
was the click itself and the four writes that follow it, because exercising
them through the full pipeline means a language model answering twelve
questions, and at the wrong moment that is twelve minutes and somebody's API
quota.

So this drives the real browser against the real form with the real
`find_advance`, and skips the model. What it proves is narrow and exactly the
gap: that the submit control is found rather than mistaken for a "next" link,
that pressing it reaches the server, and that a submission is recorded before
anything that could fail is attempted.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")

from applier.db import Database, now  # noqa: E402

FORM = """<!doctype html><html><body>
<form method="POST" action="/apply" id="application">
  <label for="first_name">First name</label>
  <input type="text" id="first_name" name="first_name">
  <label for="email">Email</label>
  <input type="email" id="email" name="email">
  <!-- A decoy: a link that advances but does not submit. -->
  <a href="#" id="save-draft">Save draft</a>
  <button type="submit" id="submit-application">Submit application</button>
</form></body></html>"""


@pytest.fixture
def served():
    """The fake employer, so the POST has somewhere real to land."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "fake_employer",
        Path(__file__).resolve().parent.parent / "scripts" / "fake_employer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RECEIVED.clear()

    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", module.RECEIVED
    finally:
        server.shutdown()
        server.server_close()


def test_the_submit_button_is_found_and_not_confused_with_a_draft_link():
    """find_advance must report is_submit, or auto mode clicks "Save draft"."""
    from playwright.sync_api import sync_playwright

    from applier.apply.universal import UniversalFiller

    filler = UniversalFiller.__new__(UniversalFiller)   # no model needed
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(FORM)
            selector, is_submit = filler.find_advance(page)
        finally:
            browser.close()

    assert selector, "no advance control found on a form with a submit button"
    assert is_submit is True, (
        f"{selector!r} was not recognised as submit; in auto mode this means "
        f"clicking something else and never sending the application")
    assert "save-draft" not in selector


def test_pressing_submit_actually_reaches_the_employer(served, tmp_path):
    """The click itself, end to end, against a server that records the POST.

    Every required field is filled, because the form validates like a real
    one: the first version of this test left some empty and the browser
    silently refused to submit, which is itself worth knowing -- an
    application with a missed required field never leaves the page.
    """
    from playwright.sync_api import sync_playwright

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% a real enough file\n")

    base, received = served
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base}/apply", wait_until="load")
            page.fill("#first_name", "Testy")
            page.fill("#last_name", "McTestface")
            page.fill("#email", "someone@example.com")
            page.fill("#school", "Example University")
            page.fill("#why", "Because the work is interesting.")
            # The form grew a required field carrying a `pattern`, so that
            # the pipeline can be shown refusing an incomplete submit.
            # Leaving it empty here means the browser refuses outright, and
            # this test is about the click reaching the server.
            page.fill("#ref_code", "ABC-1234")
            page.select_option("#work_auth", "yes")
            page.select_option("#sponsorship", "yes")
            page.set_input_files("#resume", str(resume))
            page.click("#submit-application")
            page.wait_for_load_state("load")
        finally:
            browser.close()

    assert received, "the form was submitted but nothing reached the server"
    got = received[0]
    assert got.get("first_name") == "Testy"
    assert got.get("work_auth") == "yes"
    assert got["resume"]["looks_like_pdf"] is True, "the file did not arrive intact"


def test_a_missed_required_field_stops_the_submission(served):
    """Worth asserting, because it is how a run can look successful and not be.

    The browser refuses, the page does not navigate, and nothing reaches the
    employer -- so any report of success has to come from observing the
    server, not from the click returning.
    """
    from playwright.sync_api import sync_playwright

    base, received = served
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base}/apply", wait_until="load")
            page.fill("#first_name", "Testy")          # and nothing else
            page.click("#submit-application")
            page.wait_for_timeout(400)
        finally:
            browser.close()

    assert received == [], "an incomplete application reached the employer"


def test_a_submission_is_recorded_before_anything_that_can_fail(tmp_path):
    """The four writes that follow the click, and their order.

    The click returns, then the page settles, then a screenshot is taken,
    then the audit trail is serialised. Any of those can throw. The record
    has to exist before they are attempted, or a crash in the window marks a
    sent application as failed and the next run sends it again.
    """
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    click_at = src.index("clicked = sess.click(selector)")
    record_at = src.index("_record_submission(db, job, settings)")
    settle_at = src.index("sess.settle(1500)")

    assert click_at < record_at < settle_at, (
        "the submission is recorded after something that can fail")

    # One check now sits between the click and the record: whether the form
    # came back with its errors instead of accepting anything. It reads the
    # page, and reading a page mid-navigation throws -- which is what a
    # *successful* submit looks like. So it has to be incapable of raising:
    # the whole inspection is one try that defaults to "submitted". Without
    # that default an exception reaches the outer handler, the row is marked
    # failed, and the next run applies again to a job already sent.
    between = src[click_at:record_at]
    assert "try:" in between, "the post-click inspection must be guarded"
    default_at = between.index("submitted, why = True")
    assert default_at < between.index("try:"), (
        "the safe default has to be set before the part that might not finish")


def test_the_record_survives_a_failure_immediately_afterwards(tmp_path):
    from applier.config import Config
    from applier.pipeline import _record_submission

    db = Database(tmp_path / "t.db")
    db.run("INSERT INTO jobs(fingerprint,weak_key,source,company,title,url,"
           "first_seen_at,last_seen_at) VALUES('f','w','t','Example','Intern',"
           "'https://example.com/1',?,?)", (now(), now()))
    db.run("INSERT INTO applications(job_id,started_at,status) VALUES(1,?,'preparing')",
           (now(),))
    job = dict(db.one("SELECT * FROM jobs WHERE id=1"))

    _record_submission(db, job, Config({}))
    # Exactly what the except clause runs when the screenshot throws.
    db.run("UPDATE applications SET status='failed', failure_reason=? "
           "WHERE job_id=? AND status<>'submitted'", ("browser died", 1))

    row = db.one("SELECT status, submitted_at FROM applications WHERE job_id=1")
    assert row["status"] == "submitted"
    assert row["submitted_at"]
    assert db.one("SELECT status FROM jobs WHERE id=1")["status"] == "applied"
