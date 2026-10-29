"""Whether a form is actually ready to be submitted, and whether it was.

Two questions this program answered by assuming, and got wrong in both
directions on the same application:

    Fields answered   6
    Evidence          the page still shows 'required field' after submitting
    Status            submitted

Six fields on an Ashby application is not an application. The submit button
was clicked anyway, the form came back with its validation errors, and the
row was written as `submitted` -- which means the posting is now permanently
spent, because applications dedupe by `job_id` and nothing retries a job
already marked sent.

So: look before, and look after.

**Before.** The DOM says which fields are required and what each one holds;
the harvester has carried both since it was written and nothing read them
together. A required field left empty means the submit will bounce, and a
submit that will bounce should not be clicked -- halting for input costs a
day and keeps the posting, while clicking costs the posting.

**After.** A click is not a submission. The one thing that reliably
distinguishes them is that a form which accepted its input stops being a
form: it navigates, or it is replaced by a confirmation. A page still
showing the same fields at the same address did not accept anything.

Why this does not contradict the double-submission rule
------------------------------------------------------
The rule is that nothing after the click may walk back a submission,
because a housekeeping failure that reads as "not sent" makes the next run
send again. That still holds: `looks_submitted` reports *not* submitted only
on positive evidence that the form is still there -- the same fields, still
required, still empty, at an unchanged URL. Ambiguity resolves to
"submitted", exactly as before. What changed is that one specific, checkable
case is no longer ambiguous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A required field holding this is not filled. Select elements commonly sit
# on a placeholder option whose value is a prompt rather than an answer.
EMPTY_VALUES = {"", "-", "--", "---", "select", "select...", "select one",
                "choose", "choose...", "choose one", "please select",
                "none", "n/a", "0"}

# Text that means the page is still asking, not confirming. Matched only
# when the form is still present, because "* indicates a required field"
# appears in the footer of plenty of confirmation pages.
STILL_ASKING = (
    "is required", "required field", "this field is required",
    "please correct", "please complete", "please fill", "cannot be blank",
    "must be provided", "please enter", "there was an error",
    "an error occurred", "something went wrong",
)


@dataclass(frozen=True)
class Gap:
    """A required field that has nothing in it."""
    selector: str
    label: str

    def __str__(self) -> str:
        return self.label or self.selector


def _blank(value: Any) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return text in EMPTY_VALUES


def unfilled_required(fields: list[Any]) -> list[Gap]:
    """Required fields still holding nothing, in page order.

    A file input is excluded: the DOM reports an empty `value` for one even
    after a successful upload, because a page cannot read back a local path.
    Treating that as unfilled would halt every application that attached a
    resume, which is all of them.
    """
    gaps: list[Gap] = []
    for f in fields:
        if not getattr(f, "required", False):
            continue
        if getattr(f, "type", "") == "file":
            continue
        if _blank(getattr(f, "value", "")):
            gaps.append(Gap(getattr(f, "selector", ""),
                            (getattr(f, "question", "") or "").strip()[:90]))
    return gaps


def looks_submitted(*, form_url: str, final_url: str, page_text: str,
                    gaps_before: int, gaps_after: int) -> tuple[bool, str]:
    """Did the form accept it? Returns (submitted, why).

    False only on positive evidence that the form is still on screen. Every
    other outcome -- including anything unreadable -- is True, because an
    employer may well have the application and a second one is the worst
    thing this program can do.
    """
    text = re.sub(r"\s+", " ", str(page_text or "")).strip().lower()
    moved = _normalise(final_url) != _normalise(form_url)

    complaint = next((p for p in STILL_ASKING if p in text), "")
    # The form is still there if its required fields are still empty. That
    # is what separates a validation bounce from a confirmation page whose
    # footer happens to say "required field".
    still_a_form = gaps_after > 0 and gaps_after >= gaps_before

    if complaint and still_a_form:
        return False, (f"the form came back with {complaint!r} and "
                       f"{gaps_after} required field(s) still empty")
    if still_a_form and not moved:
        return False, (f"{gaps_after} required field(s) are still empty at the "
                       f"same address, so the form was not accepted")
    if moved:
        return True, f"the page moved to {final_url[:120]}"
    if complaint:
        return True, (f"the page mentions {complaint!r}, but no required field "
                      f"is empty -- treated as sent")
    return True, "the form is no longer asking for anything"


def _normalise(url: str) -> str:
    return re.sub(r"[#?].*$", "", str(url or "").strip().rstrip("/")).lower()
