"""SQLite datastore.

One file holds everything: discovered jobs, applications, the answer bank,
created accounts, outreach contacts, and an append-only event log.

Design notes
------------
* The answer bank is the memory that makes "ask once, remember forever" work.
  Questions are stored with a normalised key so a differently-worded version of
  the same question still matches (fuzzy matching lives in apply/answers.py).
* `applications` rows are written BEFORE the browser opens, so a crash mid-run
  can never produce a silent duplicate submission.
* `events` is append-only and is the audit trail for every answer actually
  submitted -- required by integrity.log_every_submitted_answer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 5

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ----------------------------------------------------------------- jobs ----
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT NOT NULL UNIQUE,   -- identity: ATS id, else canonical URL
    weak_key        TEXT,                   -- resemblance: company+title+location
    source          TEXT NOT NULL,          -- simplify | greenhouse | lever | manual | ...
    ats             TEXT,                   -- greenhouse | lever | ashby | workday | unknown
    ats_job_id      TEXT,
    company         TEXT NOT NULL,
    company_domain  TEXT,
    title           TEXT NOT NULL,
    location        TEXT,
    country         TEXT,
    remote          INTEGER DEFAULT 0,
    url             TEXT NOT NULL,
    apply_url       TEXT,
    description     TEXT,
    season          TEXT,
    role_kind       TEXT,                   -- internship | new_grad | full_time
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL,
    -- scoring
    score           REAL,
    score_detail    TEXT,                   -- JSON breakdown
    gate_status     TEXT,                   -- pass | rejected
    gate_reason     TEXT,
    sponsorship     TEXT,                   -- sponsors | silent | refuses
    status          TEXT NOT NULL DEFAULT 'new'
                    -- new | scored | queued | applied | skipped | rejected | expired
);
CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score   ON jobs(score DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_domain);

-- --------------------------------------------------------- applications ----
CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    started_at     TEXT NOT NULL,
    submitted_at   TEXT,
    status         TEXT NOT NULL DEFAULT 'preparing',
                   -- preparing | ready | submitted | failed | needs_input
                   -- | captcha | blocked | abandoned
    failure_reason TEXT,
    resume_path    TEXT,
    resume_sha256  TEXT,           -- what was actually attached, byte for byte
    submitted_url  TEXT,           -- the form, so you can go back and look
    final_url      TEXT,           -- where it landed after submit
    field_count    INTEGER,
    cover_path     TEXT,
    artifact_dir   TEXT,
    answers_json   TEXT,          -- every field actually submitted
    screenshots    TEXT,          -- JSON list of paths
    outcome        TEXT,          -- acknowledged | oa | interview | rejection | offer
    outcome_at     TEXT,
    UNIQUE(job_id)
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(status);

-- --------------------------------------------------------- answer bank ----
CREATE TABLE IF NOT EXISTS answers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    question_key   TEXT NOT NULL UNIQUE,  -- normalised question text
    question_text  TEXT NOT NULL,         -- as last seen, verbatim
    answer         TEXT NOT NULL,
    field_type     TEXT,                  -- text | select | radio | checkbox | file | date
    options_json   TEXT,                  -- for select/radio, the choices seen
    source         TEXT NOT NULL,         -- profile | user | llm
    is_legal       INTEGER NOT NULL DEFAULT 0,  -- 1 => never LLM-generated, never fuzzy-matched
    locked         INTEGER NOT NULL DEFAULT 0,  -- 1 => cannot be overwritten automatically
    confidence     REAL DEFAULT 1.0,
    times_used     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    last_used_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_answers_legal ON answers(is_legal);

-- questions we could not answer -- the "ask queue" you clear in one sitting
CREATE TABLE IF NOT EXISTS ask_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    question_text TEXT NOT NULL,
    field_type    TEXT,
    options_json  TEXT,
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    context       TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    UNIQUE(question_text)
);

-- ------------------------------------------------------------- accounts ----
-- Passwords are NEVER stored here. Only the keychain reference.
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    domain          TEXT NOT NULL,
    ats             TEXT,
    username        TEXT NOT NULL,
    keyring_service TEXT NOT NULL,
    keyring_key     TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT,
    notes           TEXT,
    UNIQUE(domain, username)
);

-- -------------------------------------------------------------- caps -------
CREATE TABLE IF NOT EXISTS company_caps (
    domain        TEXT PRIMARY KEY,
    applied_count INTEGER NOT NULL DEFAULT 0,
    last_applied  TEXT,
    locked_until  TEXT,          -- e.g. Optiver's 8-month global lockout
    note          TEXT
);

-- ------------------------------------------------------------ contacts ----
CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    person_key      TEXT NOT NULL UNIQUE,
    name            TEXT,
    company         TEXT,
    domain          TEXT,
    email           TEXT,
    role            TEXT,
    source          TEXT,
    profile_url     TEXT,          -- a page about them; often the only channel
    github          TEXT,          -- their handle, when that is how you reach them
    draft_subject   TEXT,          -- the written message, kept so a screen can
    draft_body      TEXT,          -- show it; the file on disk is for reading
    draft_channel   TEXT,          -- email | github | profile
    draft_target    TEXT,          -- the actual address or URL to send to
    drafted_at      TEXT,
    job_id          INTEGER,       -- the posting this was written about
    hook_fact       TEXT,
    hook_source_url TEXT,          -- a draft with no real source URL is blocked
    stage           TEXT NOT NULL DEFAULT 'harvested',
    first_contact   TEXT,
    last_contact    TEXT,
    follow_ups      INTEGER NOT NULL DEFAULT 0,
    bounces         INTEGER NOT NULL DEFAULT 0,
    next_action_at  TEXT
);

-- --------------------------------------------------------------- events ----
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    job_id     INTEGER,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT,
    data_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_at   ON events(at DESC);

-- ---------------------------------------------------------- llm quota ------
CREATE TABLE IF NOT EXISTS llm_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    task          TEXT,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON llm_usage(at DESC);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class Database:
    """One database, one connection per thread.

    A sqlite3.Connection may only be used by the thread that opened it. That
    was invisible while everything ran on one thread, and became a hard error
    the moment the GUI started running discover/apply/the autonomous loop on
    worker threads: every background task died on its first query.

    So the connection lives in thread-local storage and is opened on first use
    by each thread. WAL mode makes this safe to do — concurrent readers do not
    block a writer, and `timeout` lets a writer wait rather than fail.

    The schema is created once, by whichever thread gets there first; the
    statements are all IF NOT EXISTS, so a race is harmless.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._ready = False
        self._connect()          # fail fast here, not on first query

    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # Applies per connection, not per database: a new thread's connection
        # would otherwise default back to legacy rollback-journal behaviour.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        with self._init_lock:
            if not self._ready:
                conn.executescript(SCHEMA)
                prev = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'").fetchone()
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
                if "weak_key" not in cols:
                    conn.execute("ALTER TABLE jobs ADD COLUMN weak_key TEXT")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_weak ON jobs(weak_key)")
                # Added in version 3. Without these, a contact reachable only
                # through a profile page had no channel at all -- which is
                # every GitHub contact, and GitHub is the best structured
                # source there is, so the drafter refused all of them.
                ccols = {r[1] for r in conn.execute("PRAGMA table_info(contacts)")}
                for col in ("profile_url", "github", "draft_subject",
                            "draft_body", "draft_channel", "draft_target",
                            "drafted_at"):
                    if col not in ccols:
                        conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
                if "job_id" not in ccols:
                    conn.execute("ALTER TABLE contacts ADD COLUMN job_id INTEGER")
                # Added in version 5. The first three existed and were never
                # written to, so the app reported "no hash recorded" against a
                # file it had just sent, and there was no way to reach the
                # form afterwards.
                acols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
                #
                # Added in version 6. `status='submitted'` only ever meant
                # "click() was called on a submit selector" -- not that the
                # click landed, not that the form validated. These record
                # what actually corroborates it. See apply/confirm.py.
                for col, kind in (("submitted_url", "TEXT"), ("final_url", "TEXT"),
                                  ("field_count", "INTEGER"),
                                  ("evidence", "TEXT"), ("evidence_detail", "TEXT"),
                                  ("acknowledged_at", "TEXT")):
                    if col not in acols:
                        conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {kind}")
                self._ready = True
                self._pending_merge = bool(prev) and int(prev[0]) < 2
                self._pending_backfill = True
        self._local.conn = conn
        # Done after the connection is on the thread, because the merge issues
        # ordinary queries through it.
        if getattr(self, "_pending_backfill", False):
            self._pending_backfill = False
            try:
                self._backfill_weak_keys()
            except Exception:
                pass
        if getattr(self, "_pending_merge", False):
            self._pending_merge = False
            try:
                n = self._merge_duplicate_jobs()
                if n:
                    print(f"  merged {n} duplicate job row(s) from an older database",
                          flush=True)
            except Exception:
                pass
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        return conn if conn is not None else self._connect()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        c = self.conn
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except Exception:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, params))

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # ------------------------------------------------------------------ #
    def log(
        self,
        kind: str,
        message: str = "",
        *,
        job_id: int | None = None,
        level: str = "info",
        **data: Any,
    ) -> None:
        self.run(
            "INSERT INTO events(at,kind,job_id,level,message,data_json) VALUES(?,?,?,?,?,?)",
            (now(), kind, job_id, level, message, json.dumps(data, default=str) if data else None),
        )

    # Fields that describe a posting rather than identify it. When a posting is
    # recognised, these are refreshed -- a corrected company name or an edited
    # title should improve the row we already have.
    _MUTABLE = ("weak_key", "company", "company_domain", "title", "location", "country",
                "remote", "description", "season", "role_kind", "posted_at",
                "url", "apply_url", "fingerprint", "source", "ats", "ats_job_id")

    def _find_job(self, job: dict[str, Any]) -> int | None:
        """The same posting, however it is spelled this time.

        Identity, strongest first:

          1. the ATS's own id for it. An employer renaming itself on its board
             does not change this.
          2. the canonical URL, with tracking parameters removed.
          3. company + title + location, which is what this used to use alone.

        (3) is not an identity, it is a guess -- and acting on it alone was a
        real defect. Correcting a company name from "alphasightsresumedrop" to
        "AlphaSights" changed the fingerprint, so the next sighting inserted a
        second row for a posting already in the table. Applications dedupe by
        job_id, so a duplicate row defeats the one check standing between
        autonomous mode and applying to the same job twice.
        """
        ats, ats_id = job.get("ats"), str(job.get("ats_job_id") or "")
        if ats and ats not in ("", "unknown") and ats_id:
            # Authoritative, and deliberately without a fallback. Two postings
            # from the same board with different ids ARE different postings,
            # even when company, title and location all match -- a firm
            # advertising the same role in two cities is the ordinary case.
            # Falling through to the fingerprint here merged them, which is
            # the worse direction to fail: a duplicate row is visible, a
            # swallowed opening is not.
            row = self.one("SELECT id FROM jobs WHERE ats=? AND ats_job_id=?",
                           (ats, ats_id))
            if row:
                return int(row["id"])
            # No early return. An identified posting still has to be able to
            # fold onto an unidentified row for the same job -- an aggregator
            # lists it with its own URL and no id, the board lists it with an
            # id, and they are one job. The resemblance step below accepts
            # that and refuses the case where both rows are identified and
            # disagree, which is two openings on one team.

        row = self.one("SELECT id FROM jobs WHERE fingerprint=?", (job["fingerprint"],))
        if row:
            return int(row["id"])

        # Resemblance, accepted only against a row that has no stable id of its
        # own to contradict it. This is how an aggregator's copy of a listing
        # folds onto the board's copy; refusing it when both rows carry an ATS
        # id is what stops two openings on one team collapsing into one.
        weak = job.get("weak_key")
        if weak:
            for cand in self.q(
                    "SELECT id, ats, ats_job_id FROM jobs WHERE weak_key=? ORDER BY id",
                    (weak,)):
                their_ats = (cand["ats"] or "").lower()
                their_id = str(cand["ats_job_id"] or "")
                if their_ats and their_ats != "unknown" and their_id and ats_id \
                        and (their_ats, their_id) != (str(ats or "").lower(), ats_id):
                    continue            # both identified, and they disagree
                return int(cand["id"])
        return None

    def upsert_job(self, job: dict[str, Any]) -> int:
        """Insert, or refresh an existing posting however it is spelled today."""
        existing = self._find_job(job)
        if existing is not None:
            sets = ",".join(f"{c}=?" for c in self._MUTABLE if c in job)
            vals = [job[c] for c in self._MUTABLE if c in job]
            self.run(f"UPDATE jobs SET {sets}{',' if sets else ''}last_seen_at=? WHERE id=?",
                     tuple(vals) + (now(), existing))
            return existing
        cols = [
            "fingerprint", "weak_key", "source", "ats", "ats_job_id", "company", "company_domain",
            "title", "location", "country", "remote", "url", "apply_url", "description",
            "season", "role_kind", "posted_at",
        ]
        vals = [job.get(c) for c in cols]
        placeholders = ",".join("?" * (len(cols) + 2))
        cur = self.run(
            f"INSERT INTO jobs({','.join(cols)},first_seen_at,last_seen_at) VALUES({placeholders})",
            tuple(vals) + (now(), now()),
        )
        return int(cur.lastrowid)

    def _backfill_weak_keys(self) -> int:
        """Give older rows the resemblance key they were written without."""
        from .discover.sources import fingerprint as _weak

        rows = self.q("SELECT id, company, title, location FROM jobs "
                      "WHERE weak_key IS NULL OR weak_key = ''")
        for r in rows:
            self.run("UPDATE jobs SET weak_key=? WHERE id=?",
                     (_weak(r["company"] or "", r["title"] or "", r["location"] or ""),
                      int(r["id"])))
        return len(rows)

    def _merge_duplicate_jobs(self) -> int:
        """Fold rows that are the same posting onto one id. Runs once.

        Only ever reached for databases written before the identity fix. The
        keeper is whichever row an application already points at, because that
        is the row with history attached; otherwise the earliest sighting.
        """
        from .discover.sources import canonical_url

        groups: dict[str, list[int]] = {}
        for row in self.q("SELECT id, ats, ats_job_id, url FROM jobs ORDER BY id"):
            ats, ats_id = row["ats"], str(row["ats_job_id"] or "")
            if ats and ats not in ("", "unknown") and ats_id:
                key = f"ats:{ats}:{ats_id}"
            else:
                key = f"url:{canonical_url(row['url'] or '')}"
            if not key.endswith(":"):
                groups.setdefault(key, []).append(int(row["id"]))

        merged = 0
        for ids in groups.values():
            if len(ids) < 2:
                continue
            with_app = {int(r["job_id"]) for r in
                        self.q("SELECT job_id FROM applications WHERE job_id IN "
                               f"({','.join('?' * len(ids))})", tuple(ids))}
            keeper = min(with_app) if with_app else min(ids)
            losers = [i for i in ids if i != keeper]
            for loser in losers:
                # applications has UNIQUE(job_id): move the row only if the
                # keeper has none, otherwise the loser's row is the duplicate.
                if loser in with_app and keeper not in with_app:
                    self.run("UPDATE applications SET job_id=? WHERE job_id=?",
                             (keeper, loser))
                    with_app.add(keeper)
                self.run("UPDATE events SET job_id=? WHERE job_id=?", (keeper, loser))
                self.run("UPDATE ask_queue SET job_id=? WHERE job_id=?", (keeper, loser))
                self.run("DELETE FROM jobs WHERE id=?", (loser,))
                merged += 1
        return merged

    def close(self) -> None:
        """Close this thread's connection. Other threads keep their own."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None


_db: Database | None = None


def get_db(path: Path | None = None) -> Database:
    global _db
    if _db is None:
        from .config import project_paths

        _db = Database(path or project_paths()["db"])
    return _db
