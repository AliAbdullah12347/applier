"""How sure the system is that an application was actually sent.

`status='submitted'` has meant one thing since this program was written: a
submit selector was found and `click()` was called on it. The click's own
return value was discarded at the call site, so an element that could not be
clicked at all was recorded identically to one that reached a thank-you
page. So was a form that came straight back saying "this field is required".

That is the program's account of itself, which is the weakest evidence
there is. The employer's acknowledgement email is the only piece that does
not come from this program at all.

The two rules everything here holds to:

* **The status is never conditional on any of it.** `click()` returns False
  whenever the call raised, and it raises both when the element was missing
  and when the page navigated out from under it mid-click -- which is what a
  *successful* submit looks like. Treating False as "not sent" would apply
  again tomorrow to a job an employer already has.

* **Evidence only ever increases.** Absence of an acknowledgement is not
  absence of an application; plenty of boards never send one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applier.apply import acknowledge
from applier.apply.confirm import (
    ACKNOWLEDGED, CLICK_FAILED, CLICKED, LADDER, PAGE_CHANGED, read_page,
    stronger,
)


class FakePage:
    def __init__(self, url: str, text: str = "") -> None:
        self.url = url
        self._text = text

    def inner_text(self, _selector: str) -> str:
        return self._text

    def content(self) -> str:
        return self._text


FORM = "https://boards.example.com/jobs/42/apply"


# --------------------------------------------------------------------------- #
# reading the page
# --------------------------------------------------------------------------- #
def test_a_click_that_failed_is_recorded_as_such():
    level, detail = read_page(FakePage(FORM), form_url=FORM, clicked=False)
    assert level == CLICK_FAILED
    assert "did not accept" in detail


def test_a_confirmation_page_is_the_strong_page_signal():
    page = FakePage("https://boards.example.com/thanks",
                    "Thank you for applying to Northwind.")
    level, detail = read_page(page, form_url=FORM, clicked=True)
    assert level == PAGE_CHANGED
    assert "thank you for applying" in detail


def test_a_form_that_came_back_is_not_a_confirmation():
    """The case that makes `clicked` untrustworthy.

    A validation failure re-renders the form at the same address, and that
    is currently recorded as a submitted application.
    """
    page = FakePage(FORM, "Phone number is required. Please correct the errors below.")
    level, detail = read_page(page, form_url=FORM, clicked=True)
    assert level == CLICKED
    assert "required" in detail


def test_an_error_beats_a_boilerplate_thank_you():
    """Pages carry both: a validation summary and a footer pleasantry."""
    page = FakePage(FORM,
                    "Email is required. ... Thank you for your interest in Example Corp.")
    level, _ = read_page(page, form_url=FORM, clicked=True)
    assert level == CLICKED


def test_navigating_away_counts_even_without_a_phrase():
    page = FakePage("https://boards.example.com/jobs/42/complete", "All done.")
    level, detail = read_page(page, form_url=FORM, clicked=True)
    assert level == PAGE_CHANGED
    assert "complete" in detail


@pytest.mark.parametrize("url", [
    FORM + "/", FORM + "#top", FORM + "?src=email", FORM.upper(),
])
def test_the_same_page_is_not_a_change(url):
    """A fragment, a tracking parameter or a trailing slash is not progress."""
    level, _ = read_page(FakePage(url), form_url=FORM, clicked=True)
    assert level == CLICKED


def test_an_unreadable_page_does_not_raise():
    class Hostile:
        @property
        def url(self):
            raise RuntimeError("context destroyed")

        def inner_text(self, _s):
            raise RuntimeError("gone")

        def content(self):
            raise RuntimeError("gone")

    level, _ = read_page(Hostile(), form_url=FORM, clicked=True)
    assert level == CLICKED, "an exception here lands after the employer has the form"


# --------------------------------------------------------------------------- #
# the ladder only goes up
# --------------------------------------------------------------------------- #
def test_the_ladder_is_ordered_weakest_first():
    assert LADDER == (CLICK_FAILED, CLICKED, PAGE_CHANGED, ACKNOWLEDGED)


@pytest.mark.parametrize("lower,higher", list(zip(LADDER, LADDER[1:])))
def test_each_rung_beats_the_one_below(lower, higher):
    assert stronger(lower, higher)
    assert not stronger(higher, lower)


def test_nothing_is_stronger_than_itself():
    assert not any(stronger(level, level) for level in LADDER)


def test_a_row_from_an_older_build_can_still_be_raised():
    assert stronger(None, CLICKED) and stronger("", ACKNOWLEDGED)


# --------------------------------------------------------------------------- #
# matching the employer's mail
# --------------------------------------------------------------------------- #
def test_boilerplate_words_cannot_match_a_company():
    """"Technologies" is in half of all employers; a match on it is a match
    on nothing, and a false confirmation is worse than none."""
    assert acknowledge._words("Northwind Technologies Inc") == {"northwind"}
    assert acknowledge._words("Labs Group Holdings") == set()


def test_an_acknowledgement_is_recognised():
    assert acknowledge._is_ack("Thank you for applying", "")
    assert acknowledge._is_ack("Re: your application", "We have received your application.")


def test_a_rejection_is_not_an_acknowledgement():
    """Proof of receipt, yes. But "employer confirmed" next to a rejection
    would read as good news."""
    assert not acknowledge._is_ack(
        "Your application to Northwind",
        "Thank you for applying. Unfortunately we are not moving forward.")


def test_unrelated_mail_is_not_an_acknowledgement():
    assert not acknowledge._is_ack("Your receipt from a shop", "Order shipped.")


# --------------------------------------------------------------------------- #
# the scan, against a fake mailbox
# --------------------------------------------------------------------------- #
def _message(subject: str, sender: str, when: datetime, body: str = "") -> bytes:
    from email.message import EmailMessage
    from email.utils import format_datetime

    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["Date"] = format_datetime(when)
    m.set_content(body or "Thank you for applying.")
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = messages
        self.fetched: list[str] = []
        self.logged_out = False

    def select(self, _box, readonly=False):
        self.readonly = readonly
        return ("OK", [b""])

    def search(self, _charset, *_criteria):
        return ("OK", [b" ".join(str(i).encode() for i in range(len(self.messages)))])

    def fetch(self, num, spec):
        self.fetched.append(spec)
        return ("OK", [(b"1 (BODY[] {1})", self.messages[int(num)])])

    def logout(self):
        self.logged_out = True


@pytest.fixture
def mailbox(monkeypatch):
    def install(messages):
        box = FakeIMAP(messages)
        monkeypatch.setattr(acknowledge, "_connect", lambda s, p: box)
        return box
    return install


NOW = datetime.now(timezone.utc)
SETTINGS: dict = {}
PROFILE: dict = {}


def _pending(company="Northwind", job_id=1, when=None):
    return [{"job_id": job_id, "company": company,
             "submitted_at": (when or NOW - timedelta(hours=1)).isoformat()}]


def test_a_matching_acknowledgement_is_found(mailbox):
    mailbox([_message("Thank you for applying to Northwind",
                      "careers@example.com", NOW)])
    acks = acknowledge.scan(SETTINGS, PROFILE, _pending())
    assert [a.job_id for a in acks] == [1]


def test_mail_from_a_different_company_does_not_match(mailbox):
    mailbox([_message("Thank you for applying to Contoso",
                      "careers@example.com", NOW)])
    assert acknowledge.scan(SETTINGS, PROFILE, _pending()) == []


def test_mail_that_predates_the_submission_does_not_match(mailbox):
    """Almost always a different application to the same company -- which is
    exactly the false match that would make this worse than useless."""
    mailbox([_message("Thank you for applying to Northwind",
                      "careers@example.com", NOW - timedelta(days=30))])
    assert acknowledge.scan(SETTINGS, PROFILE, _pending()) == []


def test_one_email_cannot_confirm_two_applications(mailbox):
    mailbox([_message("Thank you for applying to Northwind",
                      "careers@example.com", NOW)])
    pending = _pending(job_id=1) + _pending(job_id=2)
    assert len(acknowledge.scan(SETTINGS, PROFILE, pending)) == 1


def test_the_scan_never_marks_your_mail_as_read(mailbox):
    """A tool that silently marks your inbox read is one you stop running."""
    box = mailbox([_message("Thank you for applying to Northwind",
                            "careers@example.com", NOW)])
    acknowledge.scan(SETTINGS, PROFILE, _pending())
    assert box.readonly is True
    assert all("PEEK" in spec for spec in box.fetched), box.fetched


def test_the_mailbox_is_always_logged_out(mailbox):
    box = mailbox([b"not a valid message at all"])
    acknowledge.scan(SETTINGS, PROFILE, _pending())
    assert box.logged_out


def test_nothing_pending_does_not_open_the_mailbox(monkeypatch):
    monkeypatch.setattr(acknowledge, "_connect",
                        lambda s, p: pytest.fail("connected with nothing to look for"))
    assert acknowledge.scan(SETTINGS, PROFILE, []) == []


# --------------------------------------------------------------------------- #
# the status is never conditional on the evidence
# --------------------------------------------------------------------------- #
def test_a_failed_click_is_still_recorded_as_submitted():
    """The expensive direction to get wrong.

    `click()` returns False when the call raised, and a page navigating away
    mid-click -- a successful submit -- raises. Reading False as "not sent"
    is how a job gets applied to twice.
    """
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    assert "clicked = sess.click(selector)" in src
    assert "submitted = True" in src
    i = src.index("clicked = sess.click(selector)")
    after = src[i:i + 600]
    assert "submitted = True" in after, "the click's result must not gate the status"
    assert "if clicked" not in after.split("submitted = True")[0], (
        "nothing may sit between the click and the row being marked submitted")
