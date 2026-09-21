"""The GUI's HTTP API.

Every endpoint here is a thin shell over the same functions the CLI calls.
That is the whole design rule: the GUI must not grow a second implementation
of anything, because two implementations of "should this application be
submitted" is how a system ends up submitting something it shouldn't.

Where a CLI command blocks (discover, apply, run), the endpoint starts a
background task and returns its id; the browser tails the log over SSE.

Read handlers return `(status, payload)`. Anything that raises is turned into
a JSON error by the server, so handlers do not carry try/except boilerplate.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .. import autonomy
from ..config import (
    CONFIG_DIR, Config, ConfigError, get_secret, load_profile, load_settings,
    patch_settings, project_paths, redact, save_profile, set_secret, unanswered,
)
from ..db import get_db
from .tasks import RUNNER, Task, console_for

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
PATHS = project_paths()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _cfg() -> tuple[Config, Config]:
    try:
        return load_settings(reload=True), load_profile(reload=True)
    except ConfigError as e:
        raise ApiError(str(e), 503)


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


def _jloads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
def get_state(_body=None, **_kw) -> dict:
    """Everything the home screen shows, in one round trip."""
    db = get_db()
    settings, profile = _cfg()

    def n(sql, params=()):
        r = db.one(sql, params)
        return int(list(r)[0]) if r else 0

    funnel = {
        "discovered": n("SELECT COUNT(*) FROM jobs"),
        "gate_rejected": n("SELECT COUNT(*) FROM jobs WHERE gate_status='rejected'"),
        "queued": n("SELECT COUNT(*) FROM jobs WHERE status='queued'"),
        "skipped": n("SELECT COUNT(*) FROM jobs WHERE status='skipped'"),
        "started": n("SELECT COUNT(*) FROM applications"),
        "submitted": n("SELECT COUNT(*) FROM applications WHERE status='submitted'"),
        "needs_input": n("SELECT COUNT(*) FROM applications WHERE status IN ('needs_input','captcha')"),
        "failed": n("SELECT COUNT(*) FROM applications WHERE status='failed'"),
    }
    outcomes = {r["outcome"]: r["c"] for r in db.q(
        "SELECT outcome, COUNT(*) c FROM applications "
        "WHERE outcome IS NOT NULL GROUP BY outcome")}

    lvl = autonomy.current(settings)
    return {
        "funnel": funnel,
        "outcomes": outcomes,
        "open_asks": n("SELECT COUNT(*) FROM ask_queue WHERE resolved_at IS NULL"),
        "contacts": n("SELECT COUNT(*) FROM contacts"),
        "drafted": n("SELECT COUNT(*) FROM contacts WHERE stage='drafted'"),
        "answers_learned": n("SELECT COUNT(*) FROM answers"),
        "llm_calls": n("SELECT COUNT(*) FROM llm_usage"),
        "llm_failures": n("SELECT COUNT(*) FROM llm_usage WHERE ok=0"),
        "profile_gaps": unanswered(profile),
        "autonomy": {"level": lvl, "levels": autonomy.describe()},
        "tasks": RUNNER.list()[:8],
        "recent": _rows(db.q(
            "SELECT at, kind, level, message FROM events ORDER BY id DESC LIMIT 25")),
        "top": _rows(db.q(
            "SELECT id, company, title, location, score, url, status "
            "FROM jobs WHERE status='queued' ORDER BY score DESC LIMIT 8")),
    }


def get_events(_body=None, *, query=None, **_kw) -> dict:
    q = query or {}
    limit = min(int(q.get("limit", 200)), 1000)
    kind = q.get("kind")
    db = get_db()
    if kind:
        rs = db.q("SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
    else:
        rs = db.q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return {"events": _rows(rs)}


def run_doctor(_body=None, **_kw) -> dict:
    """The same checks as `applier doctor`, as structured data.

    Includes a real LLM call: a key that is present but rejected looks
    identical to a working one until something tries to use it, and finding
    that out during an application is the wrong time.
    """
    checks: list[dict] = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    try:
        settings, profile = _cfg()
        add("settings.yaml", True, settings.source)
        add("profile.yaml", True, profile.source)
    except ApiError as e:
        add("configuration", False, e.message)
        return {"checks": checks, "ok": False}

    gaps = unanswered(profile)
    add("profile complete", not gaps,
        "all fields answered" if not gaps else f"{len(gaps)} unanswered: " + ", ".join(gaps[:6]))

    env = settings.get("llm.primary.api_key_env", "GEMINI_API_KEY")
    key = get_secret(env)
    add(f"API key ({env})", bool(key),
        "found in keychain or environment" if key else "not set — Settings > API key")

    add("pdflatex", bool(_which("pdflatex")), _which("pdflatex") or "not on PATH — resume cannot render")

    try:
        import playwright  # noqa: F401
        add("playwright", True, "installed")
    except ImportError:
        add("playwright", False, "pip install playwright && playwright install chromium")

    dbp = PATHS["db"]
    add("database", dbp.exists() or dbp.parent.exists(), str(dbp))

    bank_dir = CONFIG_DIR / "bank"
    atoms = bank_dir / "atoms.yaml"
    if atoms.exists():
        try:
            from ..tailor.bank import Bank
            b = Bank(bank_dir)
            problems = b.lint()
            add("content bank", not problems,
                f"{len(b.atoms)} phrasings, {len(problems)} lint problem(s)")
        except Exception as e:
            add("content bank", False, str(e))
    else:
        add("content bank", False, "not built — Resume tab > Rebuild")

    if key:
        try:
            from ..llm.router import Router
            r = Router(settings, profile, get_db())
            t0 = time.time()
            resp = r.complete("You are a health check.",
                              "Reply with the single word: ok", task="doctor")
            text = getattr(resp, "text", resp) or ""
            add("live LLM call", "ok" in str(text).lower(),
                f"{settings.get('llm.primary.model')} responded in {time.time()-t0:.1f}s")
        except Exception as e:
            add("live LLM call", False, str(e)[:300])

    return {"checks": checks, "ok": all(c["ok"] for c in checks)}


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
SORTS = {"score": "score DESC NULLS LAST", "new": "id DESC",
         "company": "company COLLATE NOCASE", "seen": "last_seen_at DESC"}


def list_jobs(_body=None, *, query=None, **_kw) -> dict:
    q = query or {}
    limit = min(int(q.get("limit", 100)), 500)
    offset = max(int(q.get("offset", 0)), 0)
    status = q.get("status", "")
    search = (q.get("q") or "").strip()
    sort = SORTS.get(q.get("sort", "score"), SORTS["score"])

    where, params = ["1=1"], []
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if search:
        where.append("(company LIKE ? OR title LIKE ? OR location LIKE ?)")
        params += [f"%{search}%"] * 3
    clause = " AND ".join(where)

    db = get_db()
    total = db.one(f"SELECT COUNT(*) c FROM jobs WHERE {clause}", tuple(params))["c"]
    rs = db.q(
        f"SELECT j.id, j.company, j.title, j.location, j.country, j.remote, j.url, "
        f"j.apply_url, j.ats, j.source, j.score, j.status, j.gate_status, j.gate_reason, "
        f"j.sponsorship, j.posted_at, j.first_seen_at, "
        f"(j.description IS NULL OR LENGTH(j.description) < 200) AS thin, "
        f"a.id AS application_id, a.status AS application_status "
        f"FROM jobs j LEFT JOIN applications a ON a.job_id = j.id "
        f"WHERE {clause} ORDER BY {sort} LIMIT ? OFFSET ?",
        tuple(params) + (limit, offset))
    counts = {r["status"]: r["c"] for r in db.q(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    return {"jobs": _rows(rs), "total": total, "offset": offset,
            "limit": limit, "counts": counts}


def get_job(_body=None, *, job_id: int, **_kw) -> dict:
    db = get_db()
    row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ApiError("no such job", 404)
    job = dict(row)
    job["score_detail"] = _jloads(job.get("score_detail"), {})
    app = db.one("SELECT * FROM applications WHERE job_id=?", (job_id,))
    job["application"] = dict(app) if app else None
    return {"job": job}


def set_job_status(body, *, job_id: int, **_kw) -> dict:
    allowed = {"new", "scored", "queued", "skipped", "applied", "expired"}
    status = (body or {}).get("status")
    if status not in allowed:
        raise ApiError(f"status must be one of {sorted(allowed)}")
    db = get_db()
    if not db.one("SELECT 1 FROM jobs WHERE id=?", (job_id,)):
        raise ApiError("no such job", 404)
    db.run("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    db.log("job_status_changed", job_id=job_id, message=f"-> {status}")
    return {"ok": True, "status": status}


# --------------------------------------------------------------------------- #
# tasks: the long-running verbs
# --------------------------------------------------------------------------- #
def _spawn(kind: str, name: str, fn) -> dict:
    try:
        task = RUNNER.start(name, kind, fn)
    except RuntimeError as e:
        raise ApiError(str(e), 409)
    return {"task": task.public()}


def start_discover(body=None, **_kw) -> dict:
    limit = int((body or {}).get("limit") or 0) or None

    def work(task: Task):
        from ..pipeline import discover_jobs
        settings, profile = _cfg()
        con = console_for(task)
        n = discover_jobs(settings, profile, limit=limit, console=con)
        task.checkpoint()
        return f"{n} new postings stored"

    return _spawn("discover", "Discover postings", work)


def start_rank(body=None, **_kw) -> dict:
    enrich = bool((body or {}).get("enrich", True))

    def work(task: Task):
        from ..pipeline import rank_jobs
        settings, profile = _cfg()
        rows = rank_jobs(settings, profile, enrich=enrich, console=console_for(task))
        return f"{len(rows)} jobs ranked"

    return _spawn("rank", "Score and rank", work)


def start_apply(body=None, **_kw) -> dict:
    """One-shot on a pasted link, at an explicitly chosen autonomy level."""
    body = body or {}
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ApiError("paste a full http(s) job URL")
    level = body.get("level")

    def work(task: Task):
        from ..pipeline import apply_to_url
        settings, profile = _cfg()
        if level:
            lvl = autonomy.apply_level(settings, level)
            task.emit(f"autonomy: {lvl.label} — {lvl.blurb}")
        return apply_to_url(url, settings, profile, console=console_for(task))

    return _spawn("apply", f"Apply: {url[:70]}", work)


def start_apply_job(body=None, *, job_id: int, **_kw) -> dict:
    """Apply to an already-discovered posting, by id."""
    level = (body or {}).get("level")
    db = get_db()
    row = db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ApiError("no such job", 404)
    if db.one("SELECT 1 FROM applications WHERE job_id=?", (job_id,)):
        raise ApiError("an application already exists for this job", 409)
    job = dict(row)

    def work(task: Task):
        from ..pipeline import _execute_application
        settings, profile = _cfg()
        if level:
            lvl = autonomy.apply_level(settings, level)
            task.emit(f"autonomy: {lvl.label}")
        con = console_for(task)
        con.print(f"{job['company']} — {job['title']}")
        return _execute_application(job, settings, profile, get_db(), console=con)

    return _spawn("apply", f"Apply: {job['company']} — {job['title']}"[:80], work)


def start_run(body=None, **_kw) -> dict:
    """The autonomous loop, stoppable from the browser."""
    body = body or {}
    max_apps = int(body.get("max_apps") or 0) or None
    once = bool(body.get("once"))
    level = body.get("level")

    def work(task: Task):
        from ..pipeline import autonomous_loop
        settings, profile = _cfg()
        if level:
            lvl = autonomy.apply_level(settings, level)
            task.emit(f"autonomy: {lvl.label} — {lvl.blurb}")
        autonomous_loop(settings, profile, max_apps=max_apps, once=once,
                        console=console_for(task),
                        should_stop=lambda: task.stop_requested)
        return "loop finished"

    return _spawn("run", "Autonomous loop", work)


def list_tasks(_body=None, **_kw) -> dict:
    return {"tasks": RUNNER.list()}


def get_task(_body=None, *, task_id: str, **_kw) -> dict:
    t = RUNNER.get(task_id)
    if not t:
        raise ApiError("no such task", 404)
    lines, cursor = t.since(0)
    return {"task": t.public(), "lines": lines, "cursor": cursor}


def stop_task(_body=None, *, task_id: str, **_kw) -> dict:
    t = RUNNER.get(task_id)
    if not t:
        raise ApiError("no such task", 404)
    t.request_stop()
    return {"ok": True, "task": t.public()}


# --------------------------------------------------------------------------- #
# applications
# --------------------------------------------------------------------------- #
def list_applications(_body=None, *, query=None, **_kw) -> dict:
    q = query or {}
    status = q.get("status", "")
    where, params = ["1=1"], []
    if status and status != "all":
        where.append("a.status=?")
        params.append(status)
    db = get_db()
    rs = db.q(
        f"SELECT a.*, j.company, j.title, j.location, j.url, j.score "
        f"FROM applications a JOIN jobs j ON j.id=a.job_id "
        f"WHERE {' AND '.join(where)} ORDER BY a.id DESC LIMIT 300", tuple(params))
    counts = {r["status"]: r["c"] for r in db.q(
        "SELECT status, COUNT(*) c FROM applications GROUP BY status")}
    return {"applications": _rows(rs), "counts": counts}


def get_application(_body=None, *, app_id: int, **_kw) -> dict:
    db = get_db()
    row = db.one(
        "SELECT a.*, j.company, j.title, j.location, j.url, j.apply_url, j.score, "
        "j.description FROM applications a JOIN jobs j ON j.id=a.job_id WHERE a.id=?",
        (app_id,))
    if not row:
        raise ApiError("no such application", 404)
    app = dict(row)
    app["answers"] = _jloads(app.get("answers_json"), [])
    app["screenshots"] = _relativise(_jloads(app.get("screenshots"), []))
    app["artifacts"] = _artifact_listing(app.get("artifact_dir"))
    app["events"] = _rows(db.q(
        "SELECT at, kind, level, message FROM events WHERE job_id=? ORDER BY id",
        (app["job_id"],)))
    return {"application": app}


# Deliberately excludes .html. Saved employer pages land in artifact
# directories, and a blob: document inherits the origin of the page that
# created it — the origin holding this session's token. Serving one as
# text/plain is a mitigation; not serving it at all is a boundary.
SAFE_SUFFIXES = {".pdf", ".txt", ".tex", ".json", ".png", ".jpg", ".jpeg", ".md", ".log"}


def _artifact_listing(artifact_dir: str | None) -> list[dict]:
    """List a run's output files, as browser-safe relative paths.

    Paths handed to the client are always relative to the artifacts root and
    are re-resolved against it on the way back in. Returning absolute paths
    would invite the client to ask for one of its choosing.
    """
    if not artifact_dir:
        return []
    root = PATHS["artifacts"].resolve()
    d = Path(artifact_dir)
    d = (root / d).resolve() if not d.is_absolute() else d.resolve()
    if not _within(d, root) or not d.is_dir():
        return []
    out = []
    for f in sorted(d.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in SAFE_SUFFIXES:
            continue
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        out.append({"name": f.name, "rel": rel.as_posix(),
                    "size": f.stat().st_size, "suffix": f.suffix.lower()})
    return out


def _relativise(paths: list) -> list[str]:
    """Normalise stored screenshot paths to artifact-root-relative form.

    Rows written before paths were stored relative hold absolute ones, and the
    artifact endpoint refuses those by design. Rather than leave the history
    unviewable, they are converted here; anything that genuinely points
    outside the artifacts root is dropped rather than rewritten, because a
    path outside the root is not a screenshot of ours.
    """
    root = PATHS["artifacts"].resolve()
    out: list[str] = []
    for p in paths or []:
        try:
            q = Path(str(p))
            q = (root / q) if not q.is_absolute() else q
            out.append(q.resolve().relative_to(root).as_posix())
        except (ValueError, OSError):
            continue
    return out


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def set_outcome(body, *, app_id: int, **_kw) -> dict:
    allowed = {"acknowledged", "oa", "interview", "rejection", "offer", "ghosted", ""}
    outcome = (body or {}).get("outcome", "")
    if outcome not in allowed:
        raise ApiError(f"outcome must be one of {sorted(allowed - {''})} or empty")
    db = get_db()
    if not db.one("SELECT 1 FROM applications WHERE id=?", (app_id,)):
        raise ApiError("no such application", 404)
    from ..db import now
    db.run("UPDATE applications SET outcome=?, outcome_at=? WHERE id=?",
           (outcome or None, now() if outcome else None, app_id))
    db.log("outcome_recorded", message=f"application {app_id} -> {outcome or 'cleared'}")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# asks and the answer bank
# --------------------------------------------------------------------------- #
def list_asks(_body=None, **_kw) -> dict:
    db = get_db()
    rs = db.q(
        "SELECT q.*, j.company, j.title FROM ask_queue q "
        "LEFT JOIN jobs j ON j.id=q.job_id "
        "WHERE q.resolved_at IS NULL ORDER BY q.id")
    asks = _rows(rs)
    for a in asks:
        a["options"] = _jloads(a.get("options_json"), [])
    settings, profile = _cfg()
    return {"asks": asks, "profile_gaps": unanswered(profile)}


def answer_ask(body, *, ask_id: int, **_kw) -> dict:
    """Record an answer once. It is reused forever after."""
    answer = (body or {}).get("answer", "")
    if answer == "" or answer is None:
        raise ApiError("an answer is required")
    db = get_db()
    row = db.one("SELECT * FROM ask_queue WHERE id=?", (ask_id,))
    if not row:
        raise ApiError("no such question", 404)

    settings, profile = _cfg()
    from ..apply.answers import AnswerBank
    bank = AnswerBank(db, profile, settings)
    bank.remember(row["question_text"], answer,
                  field_type=row["field_type"] or "text", source="user")
    from ..db import now
    db.run("UPDATE ask_queue SET resolved_at=? WHERE id=?", (now(), ask_id))
    db.log("ask_answered", message=row["question_text"][:200])
    return {"ok": True}


def dismiss_ask(_body=None, *, ask_id: int, **_kw) -> dict:
    from ..db import now
    db = get_db()
    db.run("UPDATE ask_queue SET resolved_at=? WHERE id=?", (now(), ask_id))
    return {"ok": True}


def list_answers(_body=None, *, query=None, **_kw) -> dict:
    q = query or {}
    search = (q.get("q") or "").strip()
    db = get_db()
    if search:
        rs = db.q("SELECT * FROM answers WHERE question_text LIKE ? OR answer LIKE ? "
                  "ORDER BY is_legal DESC, times_used DESC LIMIT 400",
                  (f"%{search}%", f"%{search}%"))
    else:
        rs = db.q("SELECT * FROM answers ORDER BY is_legal DESC, times_used DESC LIMIT 400")
    return {"answers": _rows(rs)}


def update_answer(body, *, answer_id: int, **_kw) -> dict:
    body = body or {}
    db = get_db()
    row = db.one("SELECT * FROM answers WHERE id=?", (answer_id,))
    if not row:
        raise ApiError("no such answer", 404)
    sets, params = [], []
    if "answer" in body:
        sets.append("answer=?")
        params.append(str(body["answer"]))
    if "locked" in body:
        sets.append("locked=?")
        params.append(1 if body["locked"] else 0)
    if not sets:
        raise ApiError("nothing to update")
    params.append(answer_id)
    db.run(f"UPDATE answers SET {', '.join(sets)} WHERE id=?", tuple(params))
    db.log("answer_edited", message=row["question_text"][:200])
    return {"ok": True}


def delete_answer(_body=None, *, answer_id: int, **_kw) -> dict:
    db = get_db()
    row = db.one("SELECT question_text FROM answers WHERE id=?", (answer_id,))
    if not row:
        raise ApiError("no such answer", 404)
    db.run("DELETE FROM answers WHERE id=?", (answer_id,))
    db.log("answer_deleted", message=row["question_text"][:200])
    return {"ok": True}


# --------------------------------------------------------------------------- #
# content bank / master resume
# --------------------------------------------------------------------------- #
MASTER = CONFIG_DIR / "bank" / "master_resume.md"
BANK_DIR = CONFIG_DIR / "bank"


def get_bank(_body=None, **_kw) -> dict:
    text = MASTER.read_text(encoding="utf-8") if MASTER.exists() else ""
    if not text and (BANK_DIR / "master_resume.example.md").exists():
        text = (BANK_DIR / "master_resume.example.md").read_text(encoding="utf-8")
    out: dict[str, Any] = {"master": text, "master_exists": MASTER.exists(),
                           "path": str(MASTER)}
    try:
        from ..tailor.bank import Bank
        b = Bank(BANK_DIR)
        out["stats"] = {
            "atoms": len(b.atoms),
            "pinned": sum(1 for a in b.atoms if a.pinned),
            "groups": len({a.group for a in b.atoms}),
            "claims": len(b.claims),
            "retired": sum(1 for c in b.claims.values()
                           if isinstance(c, dict)
                           and str(c.get("status", "")).upper() == "RETIRED"),
        }
        out["lint"] = b.lint()
    except Exception as e:
        out["stats"] = None
        out["lint"] = [f"bank not loadable: {e}"]
    return out


def save_bank(body, **_kw) -> dict:
    """Write master_resume.md. Ali edits this; everything else derives from it."""
    text = (body or {}).get("master")
    if not isinstance(text, str) or not text.strip():
        raise ApiError("master resume cannot be empty")
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    if MASTER.exists():
        # One level of undo. Overwriting the file that holds every bullet a
        # person has ever written, with no copy, is not a risk worth taking
        # for a textarea in a browser.
        MASTER.with_suffix(".md.bak").write_text(
            MASTER.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = MASTER.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(MASTER)
    return {"ok": True, "bytes": len(text)}


def import_bank(_body=None, **_kw) -> dict:
    """Rebuild atoms + claims from the master resume. Synchronous: it is fast."""
    if not MASTER.exists():
        raise ApiError("no master_resume.md yet — write one and save it first")
    from ..tailor.master import build_bank
    try:
        stats = build_bank(MASTER, BANK_DIR, write=True)
    except Exception as e:
        raise ApiError(f"import failed: {e}")
    result = dict(stats) if isinstance(stats, dict) else {"result": str(stats)}
    try:
        from ..tailor.bank import Bank
        result["lint"] = Bank(BANK_DIR).lint()
    except Exception as e:
        result["lint"] = [str(e)]
    return result


def preview_bank(body, **_kw) -> dict:
    """What would be selected for this job description, and why.

    The single most useful screen for trusting the tailoring: it shows the
    chosen phrasings, their relevance, and the keywords in the posting that
    nothing in the bank covers.
    """
    body = body or {}
    jd = (body.get("description") or "").strip()
    job_id = body.get("job_id")
    if job_id and not jd:
        row = get_db().one("SELECT description, title, company FROM jobs WHERE id=?", (job_id,))
        if not row:
            raise ApiError("no such job", 404)
        jd = row["description"] or ""
    if len(jd) < 40:
        raise ApiError("paste a job description (at least a paragraph)")

    from ..tailor.bank import Bank, job_family
    b = Bank(BANK_DIR)
    family = body.get("family") or job_family({"title": body.get("title", ""),
                                               "description": jd})
    budget = int(body.get("budget") or 38)
    sel = b.select(jd, family=family, line_budget=budget)

    chosen = []
    for atom, size in sel.atoms:
        chosen.append({
            "id": atom.id, "group": atom.group, "section": atom.section,
            "size": size, "pinned": atom.pinned, "tags": atom.tags,
            "text": b.resolve(atom.text(size), strict=False)[0],
        })
    return {
        "family": family, "budget": budget, "selected": chosen,
        "score": round(sel.score, 3),
        "gaps": list(sel.gaps or []),
        "dropped": sel.dropped,
        "pinned_dropped": list(sel.pinned_dropped or []),
        "claims_used": sel.claims_used,
    }


# --------------------------------------------------------------------------- #
# outreach
# --------------------------------------------------------------------------- #
def _drafts_dir() -> Path:
    """Per-contact drafts, one file each.

    `save_batch` writes a single read-through file per run, which is the right
    shape for reading in one sitting and the wrong shape for a UI that wants
    to show one person's message with a copy button. Both are written; they
    hold the same text.
    """
    return PATHS["out"] / "outreach" / "by-contact"


def list_contacts(_body=None, **_kw) -> dict:
    db = get_db()
    rs = db.q("SELECT * FROM contacts ORDER BY "
              "CASE stage WHEN 'drafted' THEN 0 WHEN 'harvested' THEN 1 ELSE 2 END, "
              "company COLLATE NOCASE, name COLLATE NOCASE LIMIT 500")
    contacts = _rows(rs)
    d = _drafts_dir()
    for c in contacts:
        f = d / f"{c['person_key']}.md"
        c["draft"] = f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None
    return {"contacts": contacts,
            "stages": sorted({c["stage"] for c in contacts}),
            "pending": sum(1 for c in contacts if c["stage"] == "harvested")}


def start_harvest(body=None, **_kw) -> dict:
    body = body or {}
    kind = body.get("kind")
    value = (body.get("value") or "").strip()
    if kind not in {"github_org", "github_repo", "team_page"}:
        raise ApiError("kind must be github_org, github_repo or team_page")
    if not value:
        raise ApiError("a value is required")
    if kind == "team_page" and not re.match(r"^https?://", value):
        raise ApiError("team page must be a full http(s) URL")

    def work(task: Task):
        from ..outreach import discover as D
        from ..outreach.discover import ContactStore
        settings, _profile = _cfg()
        con = console_for(task)
        store = ContactStore(get_db(), settings)
        if kind == "github_org":
            people = D.from_github_org(value)
        elif kind == "github_repo":
            owner, _, repo = value.partition("/")
            if not repo:
                raise ValueError("repo must look like owner/name")
            people = D.from_github_repo(owner, repo)
        else:
            people = D.from_team_page(value)
        n = 0
        for p in people:
            task.checkpoint()
            store.upsert(p)
            con.print(f"  {p.name or '(unnamed)'} — {p.role or ''} {p.company or ''}")
            n += 1
        return f"{n} contact(s) harvested"

    return _spawn("outreach", f"Harvest: {value[:60]}", work)


def start_draft(body=None, **_kw) -> dict:
    """Draft messages. Mirrors `applier outreach draft`, caps and all.

    Nothing here sends. There is no send path in this codebase at all — that
    was Ali's instruction and it is enforced by absence, not by a flag that a
    future change could flip.
    """
    limit = max(1, min(int((body or {}).get("limit") or 4), 25))

    def work(task: Task):
        from ..llm.router import Router
        from ..outreach.discover import ContactStore, Person
        from ..outreach.draft import NoHookError, draft_message, save_batch
        settings, profile = _cfg()
        con = console_for(task)
        db = get_db()
        store = ContactStore(db, settings)
        rows = store.pending(limit=limit * 3)
        if not rows:
            return "nothing to draft — harvest some contacts first"

        router = Router(settings, profile, db)
        drafts, skipped = [], []
        for row in rows:
            task.checkpoint()
            if len(drafts) >= limit:
                break
            p = Person(name=row["name"] or "", company=row["company"] or "",
                       domain=row["domain"] or "", role=row["role"] or "",
                       email=row["email"] or "", source=row["source"] or "",
                       hook_fact=row["hook_fact"] or "",
                       hook_source_url=row["hook_source_url"] or "")
            ok, why = store.can_contact(p)
            if not ok:
                skipped.append(f"{p.name}: {why}")
                continue
            try:
                d = draft_message(p, profile, settings, router)
                drafts.append(d)
                con.print(f"  drafted: {p.name} ({p.company})")
            except NoHookError:
                skipped.append(f"{p.name}: no verifiable hook — dropped rather than padded")
            except Exception as e:
                skipped.append(f"{p.name}: {e}")

        for s in skipped[:12]:
            con.print(f"  skipped {s}")
        if not drafts:
            return "nothing drafted — see the log for why each contact was skipped"

        out_dir = Path(settings.get("outreach.output_dir", "out/outreach"))
        if not out_dir.is_absolute():
            out_dir = PATHS["root"] / out_dir
        path = save_batch(drafts, out_dir, db)
        con.print(f"written to {path}")

        # Per-contact copies, so the UI can show one message at a time.
        d_dir = _drafts_dir()
        d_dir.mkdir(parents=True, exist_ok=True)
        for d in drafts:
            (d_dir / f"{d.person.key}.md").write_text(d.render(), encoding="utf-8")

        con.print("Nothing was sent. Read, edit, and send them yourself.")
        return f"{len(drafts)} draft(s) written — nothing was sent"

    return _spawn("outreach", "Draft outreach messages", work)


def set_contact_stage(body, *, contact_id: int, **_kw) -> dict:
    stage = (body or {}).get("stage")
    allowed = {"harvested", "drafted", "sent", "replied", "declined", "skipped"}
    if stage not in allowed:
        raise ApiError(f"stage must be one of {sorted(allowed)}")
    db = get_db()
    if not db.one("SELECT 1 FROM contacts WHERE id=?", (contact_id,)):
        raise ApiError("no such contact", 404)
    from ..db import now
    extra = ", last_contact=?" if stage == "sent" else ""
    params: tuple = (stage, now(), contact_id) if extra else (stage, contact_id)
    db.run(f"UPDATE contacts SET stage=?{extra} WHERE id=?", params)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# settings, profile, secrets
# --------------------------------------------------------------------------- #
def get_settings(_body=None, **_kw) -> dict:
    settings, _ = _cfg()
    return {"settings": redact(settings.raw), "source": settings.source,
            "autonomy": {"level": autonomy.current(settings),
                         "levels": autonomy.describe()}}


# Keys the GUI is allowed to write. An allow-list rather than a filter: an
# arbitrary dotted-key write is a config-injection primitive, and some of these
# values end up in a subprocess argument or a file path.
WRITABLE = {
    "autonomy.level": str,
    "apply.mode": str,
    "apply.headless": bool,
    "apply.max_per_day": int,
    "apply.max_per_hour": int,
    "apply.screenshot_every_step": bool,
    "apply.universal_filler.enabled": bool,
    "apply.universal_filler.confidence_threshold": float,
    "apply.accounts.auto_create": bool,
    "search.min_score_to_autoapply": float,
    "discovery.poll_interval_minutes": int,
    "llm.primary.provider": str,
    "llm.primary.model": str,
    "llm.primary.api_key_env": str,
    "llm.primary.temperature": float,
    "llm.primary.max_output_tokens": int,
    "llm.primary.thinking_budget": int,
    "outreach.enabled": bool,
    "outreach.max_per_week": int,
    "notify.enabled": bool,
}


def put_settings(body, **_kw) -> dict:
    body = body or {}
    updates = body.get("updates") or {}
    if not isinstance(updates, dict) or not updates:
        raise ApiError("updates must be a non-empty object of dotted keys")
    clean: dict[str, Any] = {}
    for k, v in updates.items():
        if k not in WRITABLE:
            raise ApiError(f"{k!r} is not editable from the GUI")
        # A form field populated from a redacted read, then saved unchanged,
        # would persist the mask as the real value. `redact` is careful not to
        # mask anything writable, but this is the cheap second line: a config
        # value ending in "***" is never something a person typed.
        if isinstance(v, str) and v.endswith("***"):
            raise ApiError(
                f"{k!r} looks like a redacted placeholder, not a real value — "
                f"reload the page and type it again")
        caster = WRITABLE[k]
        try:
            clean[k] = bool(v) if caster is bool else caster(v)
        except (TypeError, ValueError):
            raise ApiError(f"{k!r} must be {caster.__name__}")
    settings = patch_settings(clean)
    return {"ok": True, "settings": redact(settings.raw),
            "autonomy": {"level": autonomy.current(settings)}}


def set_autonomy(body, **_kw) -> dict:
    level = (body or {}).get("level")
    if level not in autonomy.LEVELS:
        raise ApiError(f"level must be one of {autonomy.ORDER}")
    lvl = autonomy.LEVELS[level]
    patch_settings({"autonomy.level": lvl.key, "apply.mode": lvl.apply_mode,
                    "search.min_score_to_autoapply": lvl.min_score})
    return {"ok": True, "level": lvl.key, "label": lvl.label, "blurb": lvl.blurb}


# Profile fields the GUI may write. Immigration and identity answers are here
# because Ali has to be able to correct them — but they are a fixed list, so a
# crafted request cannot introduce a *new* key that the legal resolver would
# then read as though a human had vouched for it.
PROFILE_WRITABLE = {
    "identity.full_name", "identity.first_name", "identity.last_name",
    "identity.email", "identity.email_personal", "identity.phone",
    "identity.date_of_birth", "identity.pronouns", "identity.linkedin",
    "identity.github", "identity.website",
    "address.line1", "address.line2", "address.city", "address.state",
    "address.postal_code", "address.country",
    "address.permanent.line1", "address.permanent.city",
    "address.permanent.state", "address.permanent.postal_code",
    "address.permanent.country",
    "work_authorization.citizenship", "work_authorization.visa_status",
    "work_authorization.work_permit_status",
    "work_authorization.answers.authorized_now_us",
    "work_authorization.answers.authorized_now_us_qualifier",
    "work_authorization.answers.authorized_now_uk",
    "work_authorization.answers.authorized_now_eu",
    "work_authorization.answers.authorized_now_canada",
    "work_authorization.answers.authorized_now_singapore",
    "work_authorization.answers.require_sponsorship",
    "work_authorization.answers.require_sponsorship_qualifier",
    "demographics.gender", "demographics.ethnicity", "demographics.ethnicity_detail",
    "demographics.disability_status", "demographics.veteran_status",
    "compensation.expected_hourly_usd", "compensation.expected_salary_usd",
}


def get_profile(_body=None, **_kw) -> dict:
    _, profile = _cfg()
    return {"profile": redact(profile.raw), "gaps": unanswered(profile),
            "writable": sorted(PROFILE_WRITABLE), "source": profile.source}


def put_profile(body, **_kw) -> dict:
    updates = (body or {}).get("updates") or {}
    if not isinstance(updates, dict) or not updates:
        raise ApiError("updates must be a non-empty object of dotted keys")
    _, profile = _cfg()
    changed = []
    for k, v in updates.items():
        if k not in PROFILE_WRITABLE:
            raise ApiError(f"{k!r} is not editable from the GUI")
        if not isinstance(v, (str, int, float, bool)) and v is not None:
            raise ApiError(f"{k!r} must be a simple value")
        profile.set(k, "" if v is None else v)
        changed.append(k)
    save_profile(profile)
    load_profile(reload=True)
    get_db().log("profile_updated", message=", ".join(changed)[:300])
    # Deliberately does NOT echo the profile back: the reply to a write does
    # not need to carry a date of birth and a home address with it.
    return {"ok": True, "changed": changed}


def get_secrets(_body=None, **_kw) -> dict:
    """Which keys are set. Never what they are.

    There is no endpoint that returns a secret value, because there is no
    screen that needs one. The only useful question is whether a key is
    present, and `doctor` answers whether it actually works.
    """
    settings, _ = _cfg()
    names = {settings.get("llm.primary.api_key_env", "GEMINI_API_KEY")}
    for fb in settings.get("llm.fallbacks", []) or []:
        if isinstance(fb, dict) and fb.get("api_key_env"):
            names.add(fb["api_key_env"])
    for extra in ("GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                  "GROQ_API_KEY", "OPENROUTER_API_KEY", "IMAP_PASSWORD"):
        names.add(extra)
    out = []
    for n in sorted(names):
        val = get_secret(n)
        out.append({"name": n, "set": bool(val),
                    "hint": f"…{val[-4:]}" if val and len(val) > 8 else None})
    return {"secrets": out}


def put_secret(body, **_kw) -> dict:
    body = body or {}
    name = (body.get("name") or "").strip()
    value = body.get("value") or ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
        raise ApiError("key name must look like AN_ENV_VAR_NAME")
    if not value.strip():
        raise ApiError("empty value — use the delete action to clear a key")
    set_secret(name, value.strip())
    # Log the event but never the value, not even truncated.
    get_db().log("secret_set", message=f"{name} stored in OS keychain")
    return {"ok": True, "name": name}


# --------------------------------------------------------------------------- #
# maintenance
# --------------------------------------------------------------------------- #
def run_privacy_audit(_body=None, **_kw) -> dict:
    """Run the same audit that gates a push, from the GUI."""
    script = PATHS["root"] / "scripts" / "audit_privacy.py"
    if not script.exists():
        raise ApiError("audit script not found", 500)
    proc = subprocess.run([sys.executable, str(script)], capture_output=True,
                          text=True, cwd=str(PATHS["root"]), timeout=180)
    return {"ok": proc.returncode == 0,
            "output": (proc.stdout + proc.stderr)[-20000:]}


def get_paths(_body=None, **_kw) -> dict:
    return {"paths": {k: str(v) for k, v in PATHS.items()}}
