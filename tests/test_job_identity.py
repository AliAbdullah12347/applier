"""One posting, one row — and one application.

A posting arrives repeatedly, from several sources, wearing a different
spelling each time: a tracking parameter appended by an aggregator, a company
name the board renders as its own slug, a title the employer edits a week
later. Identity was company + title + location hashed together, so every one
of those produced a second row.

That is not a tidiness problem. `_execute_application` refuses to apply twice
by looking for an existing application row `WHERE job_id = ?`, so a duplicate
job row routes straight past the only check standing between autonomous mode
and sending the same employer the same application twice.

826 duplicate rows were already present in the real database when this was
written -- more than a tenth of it -- and the change that produced the last
one was a bug fix: correcting a company name from "alphasightsresumedrop" to
"AlphaSights" changed the fingerprint, so the next sighting inserted a second
row for a posting already in the table.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applier.db import Database
from applier.discover.sources import RawJob, canonical_url


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "t.db")


def _job(**kw) -> dict:
    base = dict(company="Example", title="Software Engineering Intern",
                location="New York, NY",
                url="https://job-boards.greenhouse.io/example/jobs/123",
                source="greenhouse", ats="greenhouse", ats_job_id="123",
                description="work here")
    base.update(kw)
    return RawJob(**base).to_row()


# --------------------------------------------------------------------------- #
# canonical URL
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url,expected", [
    ("https://boards.example.com/jobs/1?utm_source=x&gh_src=y",
     "https://boards.example.com/jobs/1"),
    ("https://WWW.Example.com/Jobs/1/", "https://example.com/Jobs/1"),
    ("https://example.com/jobs/1#apply", "https://example.com/jobs/1"),
    ("https://example.com:443/jobs/1", "https://example.com/jobs/1"),
])
def test_tracking_parameters_do_not_change_a_posting(url, expected):
    assert canonical_url(url) == expected


def test_a_malformed_url_does_not_raise():
    assert canonical_url("not a url") == "not a url"
    assert canonical_url("") == ""


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
def test_the_same_posting_twice_is_one_row(db):
    a = db.upsert_job(_job())
    b = db.upsert_job(_job())
    assert a == b
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


def test_a_corrected_company_name_updates_the_row_it_already_has(db):
    """The exact bug: a fix to the company name forked the posting."""
    first = db.upsert_job(_job(company="alphasightsresumedrop"))
    second = db.upsert_job(_job(company="AlphaSights"))
    assert first == second, "correcting the company name created a second row"
    assert db.one("SELECT company FROM jobs WHERE id=?", (first,))["company"] == "AlphaSights"
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


def test_an_aggregators_tracking_parameters_do_not_fork_a_posting(db):
    plain = "https://job-boards.greenhouse.io/example/jobs/999"
    a = db.upsert_job(_job(ats="unknown", ats_job_id="", url=plain))
    b = db.upsert_job(_job(ats="unknown", ats_job_id="",
                           url=plain + "?gh_src=Simplify&utm_source=Simplify"))
    assert a == b
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


def test_an_edited_title_does_not_fork_a_posting(db):
    a = db.upsert_job(_job(title="Software Engineering Intern"))
    b = db.upsert_job(_job(title="Software Engineering Intern (Summer 2027)"))
    assert a == b
    assert db.one("SELECT title FROM jobs WHERE id=?", (a,))["title"].endswith("(Summer 2027)")


def test_two_genuinely_different_postings_stay_separate(db):
    a = db.upsert_job(_job(ats_job_id="1", url="https://x.example/jobs/1"))
    b = db.upsert_job(_job(ats_job_id="2", url="https://x.example/jobs/2"))
    assert a != b
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 2


def test_the_fingerprint_still_catches_a_posting_with_no_stable_id(db):
    """Scraped postings have no ATS id and often a session-specific URL."""
    a = db.upsert_job(_job(ats="unknown", ats_job_id="",
                           url="https://careers.example.com/x?sid=aaa", source="manual"))
    b = db.upsert_job(_job(ats="unknown", ats_job_id="",
                           url="https://careers.example.com/y?sid=bbb", source="manual"))
    assert a == b, "identical company+title+location should still be one posting"


def test_one_application_per_posting_survives_a_rename(db):
    """The consequence the identity fix exists to prevent."""
    from applier.db import now
    jid = db.upsert_job(_job(company="example-board-slug"))
    db.run("INSERT INTO applications(job_id,started_at,status) VALUES(?,?,'submitted')",
           (jid, now()))
    db.upsert_job(_job(company="Example Inc"))        # the same posting, renamed
    assert db.one("SELECT COUNT(*) c FROM applications")["c"] == 1
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


# --------------------------------------------------------------------------- #
# the one-time merge
# --------------------------------------------------------------------------- #
def test_the_merge_keeps_the_row_an_application_points_at(db):
    from applier.db import now
    keep = db.upsert_job(_job(ats_job_id="7", url="https://x.example/jobs/7"))
    # Forge the pre-fix state: a second row for the same posting.
    db.run("INSERT INTO jobs(fingerprint,source,ats,ats_job_id,company,title,url,"
           "first_seen_at,last_seen_at) VALUES('dup','greenhouse','greenhouse','7',"
           "'Example','Software Engineering Intern','https://x.example/jobs/7',?,?)",
           (now(), now()))
    dup = db.one("SELECT id FROM jobs WHERE fingerprint='dup'")["id"]
    db.run("INSERT INTO applications(job_id,started_at,status) VALUES(?,?,'submitted')",
           (dup, now()))

    merged = db._merge_duplicate_jobs()
    assert merged == 1
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    app = db.one("SELECT job_id FROM applications")
    assert app is not None, "the merge discarded a submitted application"
    surviving = db.one("SELECT id FROM jobs")["id"]
    assert app["job_id"] == surviving


def test_the_merge_is_idempotent(db):
    db.upsert_job(_job())
    assert db._merge_duplicate_jobs() == 0
    assert db._merge_duplicate_jobs() == 0


# --------------------------------------------------------------------------- #
# caps
# --------------------------------------------------------------------------- #
CAPS = {
    "google.com": {"max": 3, "window_days": 30},
    "optiver.com": {"max": 1, "window_days": 240},
    "jumptrading.com": {"max": 1, "window_days": 3650},
    "goldmansachs.com": {"max": 4, "window_days": 3650},
}


@pytest.mark.parametrize("company,domain,expected", [
    # The case that was silently broken: an employer with a cap, hosted on an ATS.
    ("Optiver",        "job-boards.greenhouse.io", "optiver.com"),
    ("Optiver US LLC", "jobs.lever.co",            "optiver.com"),
    ("Google",         "job-boards.greenhouse.io", "google.com"),
    ("Jump Trading",   "jobs.ashbyhq.com",         "jumptrading.com"),
    ("Goldman Sachs",  "jobs.smartrecruiters.com", "goldmansachs.com"),
    # Direct career sites kept working all along.
    ("Optiver",        "optiver.com",              "optiver.com"),
    # And nothing else may be swept up.
    ("AlphaSights",    "job-boards.greenhouse.io", None),
    ("Googly Eyes",    "jobs.lever.co",            None),
    ("Jumpstart",      "jobs.lever.co",            None),
])
def test_a_cap_follows_the_employer_not_the_host(company, domain, expected):
    from applier.score.gates import cap_key_for
    assert cap_key_for({"company": company, "company_domain": domain}, CAPS) == expected


def test_a_cap_is_counted_under_the_key_it_is_checked_against(db):
    """Counting under the ATS domain while checking the employer meant the
    gate read a row nothing had ever written."""
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline._bump_cap)
    assert "cap_key_for" in src, (
        "_bump_cap no longer resolves the same key check_caps looks up")


def test_an_aggregators_copy_folds_onto_the_boards_copy(db):
    """The behaviour the old single-key scheme got right, kept.

    Simplify lists a posting with no ATS id and its own URL; the Greenhouse
    board lists the same posting with an id. These are one job, and the row
    that survives should end up carrying the id.
    """
    from_aggregator = db.upsert_job(_job(
        source="simplify", ats="unknown", ats_job_id="",
        url="https://simplify.jobs/p/abc/Software-Engineering-Intern"))
    from_board = db.upsert_job(_job(
        source="greenhouse", ats="greenhouse", ats_job_id="4242",
        url="https://job-boards.greenhouse.io/example/jobs/4242"))

    assert from_aggregator == from_board, "the same posting was stored twice"
    row = db.one("SELECT ats, ats_job_id FROM jobs WHERE id=?", (from_board,))
    assert (row["ats"], row["ats_job_id"]) == ("greenhouse", "4242"), (
        "the surviving row did not take on the stronger identity")


def test_two_openings_on_one_team_are_not_collapsed(db):
    """Same company, same title, same city, two real postings."""
    a = db.upsert_job(_job(ats_job_id="100", url="https://x.example/jobs/100"))
    b = db.upsert_job(_job(ats_job_id="101", url="https://x.example/jobs/101"))
    assert a != b
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 2


def test_weak_key_is_not_unique_but_fingerprint_is(db):
    """The constraint has to permit resemblance and forbid identity collisions."""
    db.upsert_job(_job(ats_job_id="100", url="https://x.example/jobs/100"))
    db.upsert_job(_job(ats_job_id="101", url="https://x.example/jobs/101"))
    keys = db.q("SELECT weak_key, fingerprint FROM jobs")
    assert len({k["weak_key"] for k in keys}) == 1, "these postings do resemble each other"
    assert len({k["fingerprint"] for k in keys}) == 2, "but they are not the same posting"
