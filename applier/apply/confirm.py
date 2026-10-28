"""How much reason there is to believe an application was actually sent.

`status='submitted'` has meant one thing since this program was written: a
submit selector was found and `click()` was called on it. Not that the click
landed, not that the form validated, not that anything happened afterwards --
the click's own return value is discarded at the call site. A form that came
back with "this field is required" is recorded identically to one that
reached a thank-you page.

That is the system's own account of itself, and it is the weakest kind of
evidence there is. The strong kind arrives separately: the employer's
acknowledgement email. If that never comes, nothing in the record can tell
you whether anything was received.

So the status stays as it is and the *evidence* is recorded beside it.

Why the status must not become conditional
------------------------------------------
The obvious change -- only mark it submitted once something confirms it --
is wrong, and wrong in the expensive direction. `click()` returns False when
the call raised, and it raises when the element was missing *and* when the
page navigated out from under it mid-call, which is what a successful submit
looks like. Treating False as "not submitted" would make the next run apply
again to a job an employer already has. A double submission is the worst
thing this system can do.

So the rule here is the same one the applications row already follows:
**evidence only ever increases.** Nothing in this module can walk a row
back; it can only add reasons to believe.
"""

from __future__ import annotations

import re
from typing import Any

# Weakest to strongest. The order is the whole point: `stronger` compares by
# position, and every write goes through it.
CLICK_FAILED = "click_failed"
CLICKED = "clicked"
PAGE_CHANGED = "page_changed"
ACKNOWLEDGED = "acknowledged"

LADDER = (CLICK_FAILED, CLICKED, PAGE_CHANGED, ACKNOWLEDGED)

MEANING = {
    CLICK_FAILED: "the submit button could not be clicked — this may not have been sent",
    CLICKED: "the button was clicked and nothing has corroborated it",
    PAGE_CHANGED: "the page moved to a confirmation after submitting",
    ACKNOWLEDGED: "the employer sent an acknowledgement email",
}

# Phrases an application site shows once it has the form. Deliberately
# generic: a board that says something unusual simply does not match, and the
# evidence stays at `clicked`, which is the truth.
CONFIRM_PHRASES = (
    "thank you for applying", "thanks for applying", "application received",
    "we have received your application", "we've received your application",
    "your application has been received", "application submitted",
    "successfully submitted", "submission received", "thank you for your "
    "interest", "thank you for your application", "we have your application",
    "application complete", "you have applied",
)

# Phrases that mean the form came back instead of going forward. These are
# the reason `clicked` cannot be trusted: every one of these pages is
# currently recorded as a submitted application.
REJECT_PHRASES = (
    "is required", "required field", "please correct", "please complete",
    "please fill", "there was an error", "an error occurred",
    "something went wrong", "invalid email", "please enter",
    "field is required", "cannot be blank", "must be provided",
)


def stronger(current: str | None, candidate: str) -> bool:
    """Is `candidate` better evidence than `current`?

    Unknown values sort below everything known, so a row written by an older
    build is upgradeable rather than frozen.
    """
    def rank(value: str | None) -> int:
        try:
            return LADDER.index(str(value))
        except ValueError:
            return -1
    return rank(candidate) > rank(current)


def _text(page: Any) -> str:
    for attempt in ("inner_text", "content"):
        try:
            got = getattr(page, attempt)("body") if attempt == "inner_text" else page.content()
            if got:
                return str(got).lower()
        except Exception:                               # noqa: BLE001
            continue
    return ""


def _matched(haystack: str, phrases: tuple[str, ...]) -> str:
    return next((p for p in phrases if p in haystack), "")


def read_page(page: Any, *, form_url: str, clicked: bool) -> tuple[str, str]:
    """What the page says about the submit, as (level, detail).

    Never raises. Called immediately after a submit, on a page that may be
    mid-navigation, already closed, or showing something nobody anticipated.
    An exception here would propagate into the block that writes the
    application's artifacts, and that block runs after an employer already
    has the form.
    """
    if not clicked:
        return CLICK_FAILED, "the submit control did not accept a click"

    try:
        final_url = str(getattr(page, "url", "") or "")
    except Exception:                                   # noqa: BLE001
        final_url = ""
    body = _text(page)

    # Checked before the confirmation phrases, because a page can carry both:
    # a validation summary at the top and a boilerplate "thank you for your
    # interest" in the footer. The error is the one that decides.
    bad = _matched(body, REJECT_PHRASES)
    if bad:
        return CLICKED, f"the page still shows {bad!r} after submitting"

    good = _matched(body, CONFIRM_PHRASES)
    if good:
        return PAGE_CHANGED, f"the page says {good!r}"

    # A form that rejects input re-renders itself at the same address. Leaving
    # it is weaker evidence than a confirmation message, but it is the signal
    # that distinguishes "went somewhere" from "came back".
    if final_url and form_url and _normalise(final_url) != _normalise(form_url):
        return PAGE_CHANGED, f"the page moved to {final_url[:160]}"

    return CLICKED, "the page did not change and said nothing about it"


def _normalise(url: str) -> str:
    """Compare URLs without being fooled by a fragment or a trailing slash."""
    return re.sub(r"[#?].*$", "", str(url or "").strip().rstrip("/")).lower()
