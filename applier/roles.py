"""Internship, new grad, or a job you need experience for.

`role_kind` has been a column in the jobs table and a key in settings since the
beginning, and neither was ever written to or read. Every posting was recorded
as an internship because that was the dataclass default, so "search only for
internships" had no effect and a Staff Engineer opening was as eligible as a
summer programme.

That matters more than it sounds. The three kinds are gated on different
things -- an internship wants you still enrolled, a new-grad posting wants you
about to finish, a senior role wants years you do not have -- and a resume for
one reads wrong for another. A system that cannot tell them apart is applying
to all three with the same document.

Classification is title-first for the same reason job_family is: the title is
what the job *is*, and a description mentions "interns" for all sorts of
incidental reasons ("you will mentor interns" is not an internship).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

INTERNSHIP = "internship"
NEW_GRAD = "new_grad"
FULL_TIME = "full_time"

KINDS = (INTERNSHIP, NEW_GRAD, FULL_TIME)


@dataclass(frozen=True)
class Mode:
    """A search mode: which kinds of posting are in scope, and what to call it."""

    key: str
    label: str
    blurb: str
    kinds: tuple[str, ...]


# What the user picks. Deliberately two, not three: "am I looking for a summer
# placement or for a job" is the question people actually have, and new-grad
# postings belong with jobs because they are jobs.
MODES: dict[str, Mode] = {
    INTERNSHIP: Mode(
        INTERNSHIP, "Internships",
        "Summer and term internships, co-ops and placements. Gated on still "
        "being enrolled, and on the season you are targeting.",
        kinds=(INTERNSHIP,)),
    "job": Mode(
        "job", "Jobs",
        "New-grad programmes and full-time openings. Gated on your graduation "
        "date rather than your enrolment, and the resume leads with shipped "
        "work rather than coursework.",
        kinds=(NEW_GRAD, FULL_TIME)),
}

DEFAULT_MODE = INTERNSHIP

# Title phrases, strongest signal first. An internship phrase anywhere in the
# title wins: "Software Engineer Intern" is an internship, not a software
# engineer opening.
_INTERN_TITLE = (
    "intern", "internship", "co-op", "coop", "co op",
    "summer analyst", "summer associate", "summer scholar",
    "placement year", "industrial placement", "year in industry",
    "work placement", "vacation scheme", "praktikum", "stage ",
    "apprentice", "trainee programme", "trainee program",
)

_NEW_GRAD_TITLE = (
    "new grad", "new graduate", "recent graduate", "university graduate",
    "graduate programme", "graduate program", "grad program", "grad scheme",
    "graduate scheme", "entry level", "entry-level", "early career",
    "early-career", "campus hire", "rotational program", "rotational programme",
    "associate software engineer", "software engineer i", "engineer i,",
    "junior software", "graduate engineer", "graduate analyst",
    # "Junior" means one to three years in some markets, but for somebody
    # with no full-time years it is far closer to new grad than to the
    # senior bucket, and the senior check below still takes precedence.
    "junior ",
)

# Titles that are unambiguously beyond an entry-level candidate. Not used to
# classify so much as to stop a senior opening being read as a new-grad one
# because the description says "recent graduates welcome to apply".
_SENIOR_TITLE = (
    "senior", "staff ", "principal", "lead ", "manager", "director",
    "head of", "vp ", "vice president", "architect", "fellow",
    " ii", " iii", " iv", "sr.", "sr ", "distinguished",
)

# How many distinct description hits before the body may override a title that
# said nothing. Same threshold, and the same reasoning, as job_family.
_DESCRIPTION_THRESHOLD = 2

_INTERN_BODY = (
    "internship", "intern position", "intern role", "summer internship",
    "this internship", "interns will", "as an intern",
    "currently enrolled", "returning to school", "returning to university",
    "pursuing a bachelor", "pursuing a master", "rising senior",
    "rising junior", "must be enrolled",
)

_NEW_GRAD_BODY = (
    "new graduate", "recent graduate", "graduating in", "graduate program",
    "entry level", "entry-level", "early career", "0-2 years",
    "0-1 years", "no prior experience", "start your career",
)


def _norm(text: Any) -> str:
    """Lowercased and padded, from whatever a row actually holds.

    Coerced rather than assumed: a title arrives from a feed, from a scraped
    page, or from a database column that is nominally TEXT, and one of those
    will eventually hand over something that is not a string.
    """
    return re.sub(r"\s+", " ", f" {str(text or '').lower()} ")


def classify(job: dict[str, Any]) -> str:
    """Which kind of posting this is.

    Returns one of INTERNSHIP, NEW_GRAD, FULL_TIME. Never raises; an
    unclassifiable posting is FULL_TIME, which is the conservative answer
    because it is the one this candidate is least likely to be eligible for
    and therefore the one most likely to be reviewed rather than auto-applied.
    """
    title = _norm(job.get("title", ""))
    body = _norm(job.get("description", ""))

    if any(p in title for p in _INTERN_TITLE):
        return INTERNSHIP
    if any(p in title for p in _NEW_GRAD_TITLE):
        return NEW_GRAD

    senior = any(p in title for p in _SENIOR_TITLE)
    if senior:
        return FULL_TIME

    # The title said nothing either way. Let the body speak, but only with
    # corroboration -- one mention of "interns" is usually somebody describing
    # who you would be mentoring.
    if sum(1 for p in _INTERN_BODY if p in body) >= _DESCRIPTION_THRESHOLD:
        return INTERNSHIP
    if sum(1 for p in _NEW_GRAD_BODY if p in body) >= _DESCRIPTION_THRESHOLD:
        return NEW_GRAD
    return FULL_TIME


def current(settings) -> str:
    """The configured search mode, defaulting to internships."""
    mode = str(settings.get("search.mode", "") or "").strip().lower()
    return mode if mode in MODES else DEFAULT_MODE


def kinds_in_scope(settings) -> tuple[str, ...]:
    """Which role kinds the current mode is looking for."""
    return MODES[current(settings)].kinds


def in_scope(job: dict[str, Any], settings) -> bool:
    """Is this posting the kind of thing we are currently looking for?

    Reads the stored `role_kind` when there is one and classifies on the fly
    when there is not, so a database written before classification existed
    does not silently drop out of every search.
    """
    kind = str(job.get("role_kind") or "") or classify(job)
    return kind in kinds_in_scope(settings)
