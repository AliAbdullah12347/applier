"""The database has to work from a worker thread.

This was not a theoretical concern. A `sqlite3.Connection` may only be used by
the thread that opened it, and the whole codebase shared one connection opened
at import time. That was invisible for as long as everything ran on the main
thread — and it broke every single background task the moment the GUI started
running discover, apply and the autonomous loop on workers:

    sqlite3.ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread.

Found by clicking "Find jobs" in a real browser, which is later than it should
have been. These tests are the earlier place.
"""

from __future__ import annotations

import threading

import pytest

from applier.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def _job(n: int) -> dict:
    return {"fingerprint": f"fp-{n}", "source": "test", "company": f"Co {n}",
            "title": "Engineer", "url": f"https://example.com/{n}"}


def test_a_worker_thread_can_read(db):
    """The exact failure: a query issued from a thread that did not connect."""
    out: list = []
    err: list = []

    def work():
        try:
            out.append(db.one("SELECT COUNT(*) c FROM jobs")["c"])
        except Exception as e:                       # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert not err, f"a worker thread could not read: {err[0]}"
    assert out == [0]


def test_a_worker_thread_can_write(db):
    err: list = []

    def work():
        try:
            db.upsert_job(_job(1))
            db.log("from_a_worker", "hello")
        except Exception as e:                       # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert not err, f"a worker thread could not write: {err[0]}"
    # and the main thread sees it
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    assert db.one("SELECT COUNT(*) c FROM events WHERE kind='from_a_worker'")["c"] == 1


def test_many_threads_write_concurrently(db):
    """WAL plus a busy timeout, rather than a lock-and-hope."""
    errors: list = []
    n = 12

    def work(i: int):
        try:
            db.upsert_job(_job(i))
            db.log("concurrent", f"worker {i}")
        except Exception as e:                       # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)

    assert not errors, f"{len(errors)} thread(s) failed, first: {errors[0]}"
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == n


def test_wal_mode_is_set_on_every_connection(db):
    """WAL is a per-connection pragma. A worker's fresh connection would
    otherwise fall back to rollback-journal, where a reader blocks a writer."""
    modes: list = []

    def work():
        modes.append(db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower())

    main_mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    t = threading.Thread(target=work)
    t.start()
    t.join(10)
    assert main_mode == "wal"
    assert modes == ["wal"], f"worker connection is in {modes} mode, not WAL"


def test_each_thread_gets_its_own_connection(db):
    seen: dict[int, int] = {}

    def work():
        seen[threading.get_ident()] = id(db.conn)

    ts = [threading.Thread(target=work) for _ in range(3)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    seen[threading.get_ident()] = id(db.conn)
    assert len(set(seen.values())) == len(seen), "a connection was shared across threads"


def test_closing_one_thread_does_not_break_another(db):
    """The GUI closes a task's connection when the task ends; the server
    thread must not be taken down with it."""
    done = threading.Event()

    def work():
        db.run("INSERT INTO events(at,kind,level) VALUES('now','x','info')")
        db.close()          # closes only this thread's connection
        done.set()

    t = threading.Thread(target=work)
    t.start()
    assert done.wait(10)
    t.join(5)
    assert db.one("SELECT COUNT(*) c FROM events")["c"] == 1


def test_a_transaction_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with db.tx() as c:
            c.execute("INSERT INTO events(at,kind,level) VALUES('now','tx','info')")
            raise RuntimeError("boom")
    assert db.one("SELECT COUNT(*) c FROM events WHERE kind='tx'")["c"] == 0
