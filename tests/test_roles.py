"""Internship, new grad, or a job you need years for.

`role_kind` was a column in the jobs table and a key in settings from the
start, and nothing ever wrote to it or read it. Every posting was recorded as
an internship because that was the dataclass default, so "search only for
internships" did nothing and a Staff Engineer opening sat in the queue beside
a summer programme. Backfilling the real database moved 210 postings.

The three kinds are gated on different things -- enrolment, imminent
graduation, years of experience -- and a resume for one reads wrong for
another, so telling them apart is not cosmetic.
"""

from __future__ import annotations

import pytest

from applier import roles
from applier.config import Config


@pytest.mark.parametrize("title", [
    "Software Engineering Intern, Summer 2027",
    "Summer Associate, Client Service, 2027",
    "2027 Software Engineer Co-op",
    "Quantitative Research Internship",
    "Summer Analyst, Technology",
    "Industrial Placement - Software",
])
def test_an_internship_is_recognised(title):
    assert roles.classify({"title": title, "description": ""}) == roles.INTERNSHIP


@pytest.mark.parametrize("title", [
    "New Grad Software Engineer",
    "Software Engineer, University Graduate 2027",
    "Associate Software Engineer",
    "Graduate Scheme - Engineering",
    "Entry Level Data Analyst",
    "Junior Full-Stack Engineer",
])
def test_a_new_grad_posting_is_recognised(title):
    assert roles.classify({"title": title, "description": ""}) == roles.NEW_GRAD


@pytest.mark.parametrize("title", [
    "Senior Software Engineer",
    "Staff Machine Learning Engineer",
    "Principal Architect",
    "Engineering Manager",
    "Software Engineer",
    "Product Manager",
])
def test_everything_else_is_a_full_time_job(title):
    assert roles.classify({"title": title, "description": ""}) == roles.FULL_TIME


# --------------------------------------------------------------------------- #
# the traps
# --------------------------------------------------------------------------- #
def test_mentoring_interns_is_not_an_internship():
    """One incidental mention is somebody describing who you would manage."""
    job = {"title": "Software Engineer",
           "description": "You will mentor interns and review their work."}
    assert roles.classify(job) == roles.FULL_TIME


def test_a_senior_title_is_not_rescued_by_a_friendly_description():
    job = {"title": "Engineering Lead",
           "description": "Recent graduates welcome to apply. Entry level friendly."}
    assert roles.classify(job) == roles.FULL_TIME


def test_a_silent_title_lets_a_corroborated_description_decide():
    job = {"title": "Software Engineer",
           "description": ("This internship runs for twelve weeks. Interns will "
                           "ship real features. Must be currently enrolled.")}
    assert roles.classify(job) == roles.INTERNSHIP


def test_classification_never_raises():
    for job in ({}, {"title": None}, {"title": "", "description": None},
                {"title": 123, "description": []}):
        try:
            assert roles.classify(job) in roles.KINDS
        except (TypeError, AttributeError):
            pytest.fail(f"classify raised on {job!r}")


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def test_the_default_is_internships():
    assert roles.current(Config({})) == roles.INTERNSHIP


def test_an_unknown_mode_falls_back_rather_than_breaking():
    assert roles.current(Config({"search": {"mode": "nonsense"}})) == roles.INTERNSHIP


def test_job_mode_covers_new_grad_and_full_time():
    assert set(roles.MODES["job"].kinds) == {roles.NEW_GRAD, roles.FULL_TIME}


@pytest.mark.parametrize("mode,kind,expected", [
    ("internship", "internship", True),
    ("internship", "full_time", False),
    ("internship", "new_grad", False),
    ("job", "full_time", True),
    ("job", "new_grad", True),
    ("job", "internship", False),
])
def test_scope_follows_the_mode(mode, kind, expected):
    settings = Config({"search": {"mode": mode}})
    assert roles.in_scope({"role_kind": kind}, settings) is expected


def test_a_posting_with_no_stored_kind_is_classified_on_the_fly():
    """A database written before classification existed must not fall out of
    every search."""
    settings = Config({"search": {"mode": "internship"}})
    assert roles.in_scope(
        {"title": "Software Engineering Intern", "description": ""}, settings) is True
    assert roles.in_scope(
        {"title": "Senior Staff Engineer", "description": ""}, settings) is False


def test_every_mode_covers_at_least_one_kind():
    """A mode matching nothing is a search that silently returns nothing."""
    for m in roles.MODES.values():
        assert m.kinds
        for k in m.kinds:
            assert k in roles.KINDS


def test_the_kinds_are_partitioned_across_modes():
    """Every kind reachable, none in two places."""
    seen = [k for m in roles.MODES.values() for k in m.kinds]
    assert sorted(seen) == sorted(roles.KINDS)
    assert len(seen) == len(set(seen)), "a kind appears in two modes"
