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
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

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
    fingerprint     TEXT NOT NULL UNIQUE,   -- company+title+location, normalised
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
    resume_sha256  TEXT,
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
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params))

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        cur = self._conn.execute(sql, params)
        return cur.fetchone()

    def run(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

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

    def upsert_job(self, job: dict[str, Any]) -> int:
        """Insert, or refresh last_seen_at if we have seen this posting before."""
        existing = self.one("SELECT id FROM jobs WHERE fingerprint=?", (job["fingerprint"],))
        if existing:
            self.run("UPDATE jobs SET last_seen_at=? WHERE id=?", (now(), existing["id"]))
            return int(existing["id"])
        cols = [
            "fingerprint", "source", "ats", "ats_job_id", "company", "company_domain",
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

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_db: Database | None = None


def get_db(path: Path | None = None) -> Database:
    global _db
    if _db is None:
        from .config import project_paths

        _db = Database(path or project_paths()["db"])
    return _db
