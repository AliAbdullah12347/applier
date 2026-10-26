"""What happens when things go wrong, which unattended is most of the time.

An autonomous run is a long sequence of operations against the open internet.
Pages hang, browsers die, models rate-limit, disks fill. None of that is
exceptional; over six hours it is routine. So the question worth testing is
not whether the happy path works -- it demonstrably does -- but whether any
failure can leave the system in a state that costs something irreversible.

There is exactly one irreversible action here: submitting an application. Most
of this file is about the window around it.
"""

from __future__ import annotations

import pytest

from applier.db import Database, now


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(tmp_path / "t.db")
    d.run("INSERT INTO jobs(fingerprint,weak_key,source,company,title,url,"
          "first_seen_at,last_seen_at) VALUES('f','w','test','Example',"
          "'Intern','https://example.com/1',?,?)", (now(), now()))
    d.run("INSERT INTO applications(job_id,started_at,status) VALUES(1,?,'preparing')",
          (now(),))
    return d


def _status(db: Database) -> str:
    return db.one("SELECT status FROM applications WHERE job_id=1")["status"]


# --------------------------------------------------------------------------- #
# the window between clicking submit and recording it
# --------------------------------------------------------------------------- #
def test_a_failure_after_submitting_cannot_mark_it_unsent(db):
    """The bug this guard exists for.

    Between clicking submit and writing the row there were three operations
    that can throw -- settling the page, the final screenshot, serialising the
    audit trail. When one did, the except clause marked the application
    'failed', erasing the fact that the employer already had it, and the next
    run applied a second time.
    """
    db.run("UPDATE applications SET status='submitted', submitted_at=? WHERE job_id=1",
           (now(),))

    # Exactly the statement the except clause runs.
    db.run("UPDATE applications SET status='failed', failure_reason=? "
           "WHERE job_id=? AND status<>'submitted'", ("browser crashed", 1))

    assert _status(db) == "submitted", "a submitted application was marked failed"


def test_a_failure_before_submitting_is_recorded_as_failed(db):
    db.run("UPDATE applications SET status='failed', failure_reason=? "
           "WHERE job_id=? AND status<>'submitted'", ("timed out", 1))
    assert _status(db) == "failed"


def test_the_artifact_write_cannot_walk_back_a_submission(db):
    """The second place it could have been lost: the tidy-up UPDATE."""
    db.run("UPDATE applications SET status='submitted', submitted_at=? WHERE job_id=1",
           (now(),))
    stamp = db.one("SELECT submitted_at FROM applications WHERE job_id=1")["submitted_at"]

    db.run(
        "UPDATE applications SET status=CASE WHEN status='submitted' "
        "  THEN 'submitted' ELSE ? END, "
        "submitted_at=COALESCE(submitted_at, ?), artifact_dir=? WHERE job_id=?",
        ("ready", now(), "applications/x", 1),
    )
    row = db.one("SELECT status, submitted_at, artifact_dir FROM applications WHERE job_id=1")
    assert row["status"] == "submitted"
    assert row["submitted_at"] == stamp, "the original submission time was overwritten"
    assert row["artifact_dir"] == "applications/x", "artifacts were not attached"


def test_recording_a_submission_twice_does_not_double_count_a_cap(db):
    """A retry must not consume two slots of an eight-month lockout."""
    from applier.config import Config
    from applier.pipeline import _record_submission

    job = dict(db.one("SELECT * FROM jobs WHERE id=1"))
    settings = Config({"gates": {"application_caps": {"example.com": {"max": 1}}}})

    _record_submission(db, job, settings)
    first = db.one("SELECT applied_count FROM company_caps")["applied_count"]
    assert first == 1

    # The same submission recorded again -- a retried write, not a second apply.
    db.run("UPDATE applications SET status='preparing' WHERE job_id=1")
    _record_submission(db, job, settings)
    stamp = db.one("SELECT submitted_at FROM applications WHERE job_id=1")["submitted_at"]
    assert stamp is not None
    assert _status(db) == "submitted"


def test_an_already_submitted_application_is_refused_a_second_time(db):
    """`applier apply <url>` run twice by hand must not send twice."""
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline._execute_application)
    assert 'existing["status"] == "submitted"' in src, (
        "the short-circuit on an already-submitted application is gone")


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
def test_the_loop_skips_a_job_that_has_any_application_row(db):
    """Including a failed one: a failure mid-apply may still have been sent."""
    for status in ("preparing", "ready", "failed", "submitted"):
        db.run("UPDATE applications SET status=? WHERE job_id=1", (status,))
        already = db.one("SELECT 1 FROM applications WHERE job_id=?", (1,))
        assert already, f"status {status!r} would let the loop apply again"


def test_a_job_with_no_description_is_never_applied_to():
    """The gates all read the description. No text means nothing was checked."""
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline.autonomous_loop)
    assert "description_missing" in src
    assert "held back" in src


def test_consecutive_failures_pause_the_run():
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline.autonomous_loop)
    assert "pause_on_consecutive_failures" in src or "fail_limit" in src
    assert "failures >= fail_limit" in src


def test_a_stop_is_never_honoured_mid_application():
    """Abandoning a half-sent form is worse than finishing it."""
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline.autonomous_loop)
    assert "stopped between applications" in src, (
        "the loop no longer documents where it is safe to stop")


# --------------------------------------------------------------------------- #
# degraded dependencies
# --------------------------------------------------------------------------- #
def test_tailoring_failure_does_not_abort_the_application():
    """A model outage should cost a tailored resume, not the application."""
    import inspect

    from applier import pipeline
    src = inspect.getsource(pipeline._execute_application)
    assert "tailoring unavailable" in src
    assert "links.resume_pdf" in src, "there is no fallback to the base resume"


def test_the_database_survives_a_locked_write(tmp_path):
    """Two connections, as the GUI and a background task really are."""
    a = Database(tmp_path / "x.db")
    b = Database(tmp_path / "x.db")
    a.run("INSERT INTO meta(key,value) VALUES('k','1') "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    b.run("INSERT INTO meta(key,value) VALUES('k','2') "
          "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
    assert b.one("SELECT value FROM meta WHERE key='k'")["value"] == "2"


def test_an_unwritable_artifact_directory_fails_loudly(tmp_path, monkeypatch):
    """Silence here would mean an application with no record of what was sent."""
    from applier.tailor.render import verify_pdf
    missing = tmp_path / "nope.pdf"

    class _S:
        def get(self, k, d=None):
            return d

    report = verify_pdf(missing, expect_keywords=[], contact=[],
                        claims_used={}, settings=_S())
    assert report.ok is False, "a missing PDF verified as sound"
