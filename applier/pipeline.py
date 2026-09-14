"""Orchestration: discover -> rank -> tailor -> apply -> track.

Two entry points matter:

    apply_to_url(url)   one-shot. You hand it any link; it does everything.
    autonomous_loop()   leave it running. It finds work and applies on its own.

Both share the same core (`_execute_application`) so a link you paste and a job
the system found itself go through identical logic.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, project_paths
from .db import Database, get_db, now

# --------------------------------------------------------------------------- #


def _console(c=None):
    if c is not None:
        return c
    from rich.console import Console
    return Console()


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
def discover_jobs(settings: Config, profile: Config, *, limit: int | None = None,
                  console=None) -> int:
    from .discover import sources as S

    con = _console(console)
    db = get_db()
    src = settings.get("discovery.sources", {})
    added = 0
    collected: list[S.RawJob] = []

    if src.get("simplify", {}).get("enabled"):
        url = src["simplify"]["url"]
        con.print(f"[dim]simplify…[/dim]")
        try:
            collected += list(S.from_simplify(url, limit=limit))
        except Exception as e:
            con.print(f"[yellow]simplify failed:[/yellow] {e}")

    if src.get("zshah_tracker", {}).get("enabled"):
        con.print(f"[dim]zshah tracker…[/dim]")
        try:
            collected += list(S.from_csv_tracker(src["zshah_tracker"]["url"], limit=limit))
        except Exception as e:
            con.print(f"[yellow]tracker failed:[/yellow] {e}")

    for job in collected:
        if not job.company or not job.title:
            continue
        try:
            db.upsert_job(job.to_row())
            added += 1
        except Exception:
            continue
        if limit and added >= limit:
            break

    # Free company discovery: mine ATS board slugs out of the apply URLs.
    tokens = S.mine_board_tokens(collected)
    summary = {k: len(v) for k, v in tokens.items() if v}
    if summary:
        (project_paths()["data"] / "board_tokens.json").write_text(
            json.dumps({k: sorted(v) for k, v in tokens.items()}, indent=2), encoding="utf-8")
        con.print(f"[dim]discovered ATS boards: {summary}[/dim]")

    db.log("discover", f"{added} postings", added=added, boards=summary)
    return added


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #
def rank_jobs(settings: Config, profile: Config, *, console=None) -> list[dict]:
    from .score.gates import check_gates, score_job

    con = _console(console)
    db = get_db()
    rows = db.q("SELECT * FROM jobs WHERE status IN ('new','scored') ORDER BY id DESC LIMIT 4000")
    out: list[dict] = []
    min_q = float(settings.get("search.min_score_to_queue", 0.55))

    for r in rows:
        job = dict(r)
        gate = check_gates(job, settings, profile, db)
        sc = score_job(job, settings, profile, gate)
        db.run(
            "UPDATE jobs SET score=?, score_detail=?, gate_status=?, gate_reason=?, "
            "sponsorship=?, status=? WHERE id=?",
            (sc.value, json.dumps(sc.breakdown), "pass" if gate.passed else "rejected",
             gate.reason, gate.sponsorship,
             "queued" if (gate.passed and sc.value >= min_q) else
             ("rejected" if not gate.passed else "scored"),
             job["id"]),
        )
        if gate.passed and sc.value >= min_q:
            out.append({**job, "score": sc.value, "why": sc.why})

    out.sort(key=lambda j: -j["score"])
    con.print(f"[dim]scored {len(rows)}, queued {len(out)}[/dim]")
    return out


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
def apply_to_url(url: str, settings: Config, profile: Config, *, console=None) -> str:
    """One-shot on any link: fetch, gate, tailor, fill, submit."""
    from .discover import sources as S

    con = _console(console)
    db = get_db()

    con.print(f"[dim]fetching {url}[/dim]")
    raw = S.fetch_single(url)
    if raw is None:
        return f"Could not fetch {url}"

    job_id = db.upsert_job(raw.to_row())
    job = dict(db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))
    con.print(f"[bold]{job['company']}[/bold] — {job['title']}")

    from .score.gates import check_gates, score_job
    gate = check_gates(job, settings, profile, db)
    if not gate.passed:
        db.run("UPDATE jobs SET gate_status='rejected', gate_reason=? WHERE id=?",
               (gate.reason, job_id))
        return f"Gate rejected: {gate.reason}"

    sc = score_job(job, settings, profile, gate)
    con.print(f"[dim]score {sc.value:.2f} — {sc.why}; sponsorship: {gate.sponsorship}[/dim]")

    return _execute_application(job, settings, profile, db, console=con)


def _execute_application(job: dict, settings: Config, profile: Config,
                         db: Database, *, console=None) -> str:
    """The shared core. Identical for pasted links and self-found jobs."""
    from .apply.answers import AnswerBank, HaltForInput
    from .apply.browser import Session, detect_ats
    from .apply.universal import UniversalFiller
    from .llm import Router

    con = _console(console)
    mode = settings.get("apply.mode", "auto")
    job_id = job["id"]

    # Row written BEFORE the browser opens: a crash can never silently duplicate.
    existing = db.one("SELECT * FROM applications WHERE job_id=?", (job_id,))
    if existing and existing["status"] == "submitted":
        return f"Already applied on {existing['submitted_at']}"
    if not existing:
        db.run("INSERT INTO applications(job_id,started_at,status) VALUES(?,?,'preparing')",
               (job_id, now()))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch for ch in f"{job['company']}_{job['title']}" if ch.isalnum() or ch in "_- ")[:70]
    artifact = Path(settings.get("apply.artifact_dir", "applications")) / f"{stamp}__{safe.strip()}"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "job.json").write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")

    router = Router(settings, profile, db)
    bank = AnswerBank(db, profile, settings)

    # --- tailored documents ------------------------------------------------ #
    resume_path = cover_path = None
    try:
        from .tailor.pipeline import build_documents
        resume_path, cover_path = build_documents(job, settings, profile, router, artifact)
        con.print(f"[green]tailored[/green] resume -> {resume_path.name if resume_path else 'n/a'}")
    except Exception as e:
        con.print(f"[yellow]tailoring unavailable ({e}); using base resume[/yellow]")
        base = profile.get("links.resume_pdf", None)
        if base and Path(base).exists():
            resume_path = Path(base)

    if mode == "dry_run":
        db.run("UPDATE applications SET status='ready', artifact_dir=? WHERE job_id=?",
               (str(artifact), job_id))
        return f"Dry run complete. Artifacts in {artifact}"

    # --- fill the form ----------------------------------------------------- #
    sess = Session(settings)
    try:
        page = sess.start()
        target = job.get("apply_url") or job["url"]
        sess.goto(target)
        ats = detect_ats(target, page.content()[:20000])
        con.print(f"[dim]ATS: {ats}[/dim]")

        filler = UniversalFiller(bank, router, settings, profile)
        max_pages = int(settings.get("apply.universal_filler.max_pages_per_application", 12))
        submitted = False
        all_filled: dict[str, Any] = {}
        shots: list[str] = []

        for step in range(max_pages):
            sess.settle()
            try:
                res = filler.fill_page(page, job, job_id=job_id)
            except HaltForInput as e:
                db.run("UPDATE applications SET status='needs_input', failure_reason=? "
                       "WHERE job_id=?", (str(e), job_id))
                bank.enqueue_ask(e.question, job_id=job_id,
                                 context=f"{job['company']} — {job['title']}")
                return (f"Paused: a legal/immigration field needs your explicit answer.\n"
                        f"  {e.question}\nRun `applier asks` to answer it once, permanently.")

            if res.captcha:
                shot = sess.screenshot(artifact / f"captcha-{step}.png")
                db.run("UPDATE applications SET status='captcha', artifact_dir=? WHERE job_id=?",
                       (str(artifact), job_id))
                return (f"CAPTCHA at step {step}. Browser left open for you to clear it.\n"
                        f"  screenshot: {shot}")

            all_filled.update(res.filled)
            if res.mismatches:
                con.print(f"[yellow]{len(res.mismatches)} field(s) did not take[/yellow]")

            # resume upload, wherever it appears
            if resume_path:
                for sel in ('input[type=file]',):
                    if sess.upload(sel, resume_path):
                        break

            shot = sess.screenshot(artifact / f"step-{step}.png")
            if shot:
                shots.append(str(shot))

            selector, is_submit = filler.find_advance(page)
            if not selector:
                break
            if is_submit:
                if mode == "review":
                    db.run("UPDATE applications SET status='ready', artifact_dir=?, "
                           "answers_json=? WHERE job_id=?",
                           (str(artifact), json.dumps(all_filled), job_id))
                    return ("Form is filled and ready. Review the open browser and click "
                            "submit yourself.")
                sess.click(selector)
                submitted = True
                break
            if not sess.click(selector):
                break

        sess.settle(1500)
        final_shot = sess.screenshot(artifact / "final.png")
        if final_shot:
            shots.append(str(final_shot))

        status = "submitted" if submitted else "ready"
        db.run(
            "UPDATE applications SET status=?, submitted_at=?, artifact_dir=?, "
            "answers_json=?, screenshots=?, resume_path=? WHERE job_id=?",
            (status, now() if submitted else None, str(artifact),
             json.dumps(all_filled), json.dumps(shots),
             str(resume_path) if resume_path else None, job_id),
        )
        if submitted:
            db.run("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
            _bump_cap(db, job)
            db.log("applied", f"{job['company']} — {job['title']}", job_id=job_id)

        return (f"{'Submitted' if submitted else 'Filled but not submitted'}: "
                f"{job['company']} — {job['title']}\n  artifacts: {artifact}")

    except Exception as e:
        db.run("UPDATE applications SET status='failed', failure_reason=? WHERE job_id=?",
               (str(e)[:400], job_id))
        db.log("apply_failed", str(e)[:200], job_id=job_id, level="error")
        raise
    finally:
        if settings.get("apply.mode") != "review":
            sess.close()


def _bump_cap(db: Database, job: dict) -> None:
    dom = (job.get("company_domain") or "").lower()
    if not dom:
        return
    db.run(
        "INSERT INTO company_caps(domain,applied_count,last_applied) VALUES(?,1,?) "
        "ON CONFLICT(domain) DO UPDATE SET applied_count=applied_count+1, last_applied=excluded.last_applied",
        (dom, now()),
    )


# --------------------------------------------------------------------------- #
# autonomous loop
# --------------------------------------------------------------------------- #
def autonomous_loop(settings: Config, profile: Config, *, max_apps: int | None = None,
                    once: bool = False, console=None) -> None:
    con = _console(console)
    db = get_db()
    cap = max_apps or int(settings.get("apply.max_per_day", 25))
    per_hour = int(settings.get("apply.max_per_hour", 6))
    interval = int(settings.get("discovery.poll_interval_minutes", 30)) * 60
    fail_limit = int(settings.get("schedule.pause_on_consecutive_failures", 3))

    done = 0
    failures = 0
    con.print(f"[bold green]autonomous mode[/bold green] — up to {cap} applications, "
              f"{per_hour}/hour. Ctrl-C to stop.")

    while True:
        try:
            discover_jobs(settings, profile, console=con)
            queued = rank_jobs(settings, profile, console=con)
        except Exception as e:
            con.print(f"[yellow]discovery cycle failed: {e}[/yellow]")
            queued = []

        threshold = float(settings.get("search.min_score_to_autoapply", 0.70))
        batch = [j for j in queued if j["score"] >= threshold][:per_hour]
        if not batch:
            con.print("[dim]nothing above the auto-apply threshold this cycle[/dim]")

        for job in batch:
            if done >= cap:
                con.print(f"[green]daily cap reached ({cap}).[/green]")
                return
            already = db.one("SELECT 1 FROM applications WHERE job_id=?", (job["id"],))
            if already:
                continue
            con.print(f"\n[bold]{job['company']}[/bold] — {job['title']}  "
                      f"[dim]({job['score']:.2f})[/dim]")
            try:
                msg = _execute_application(job, settings, profile, db, console=con)
                con.print(f"  {msg.splitlines()[0]}")
                done += 1
                failures = 0
            except Exception as e:
                failures += 1
                con.print(f"  [red]failed:[/red] {e}")
                if failures >= fail_limit:
                    con.print(f"[red]{failures} consecutive failures — pausing.[/red]")
                    return
            time.sleep(max(5, 3600 // max(1, per_hour)))

        if once:
            return
        con.print(f"[dim]sleeping {interval // 60} min[/dim]")
        time.sleep(interval)
