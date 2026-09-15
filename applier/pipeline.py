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
# enrich
# --------------------------------------------------------------------------- #
def enrich_descriptions(settings: Config, *, limit: int = 60, console=None) -> int:
    """Fetch full job descriptions for promising postings.

    This is not optional polish. Aggregate feeds carry only title, company,
    location and URL -- no description. Every eligibility gate that matters
    (citizenship requirements, security clearance, ITAR, explicit "we do not
    sponsor") is expressed in the description text, so gating on a feed row
    alone silently passes everything. Enrichment happens BEFORE final gating.

    Only postings whose title already looks relevant are fetched, so this costs
    a few dozen requests rather than thousands.
    """
    from .discover import sources as S

    con = _console(console)
    db = get_db()

    # Target the postings most likely to matter, not simply the newest. Fetching
    # by recency wastes the budget on roles that would never be queued anyway and
    # leaves the actual candidates ungated.
    includes = [t.lower() for t in settings.get("search.titles_include", []) if t]
    if includes:
        clause = " OR ".join("LOWER(title) LIKE ?" for _ in includes)
        params = tuple(f"%{t}%" for t in includes) + (limit,)
        rows = db.q(
            "SELECT id,url,ats FROM jobs "
            "WHERE (description IS NULL OR length(description) < 200) "
            f"AND status IN ('new','scored','queued') AND ({clause}) "
            "ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END, id DESC "
            "LIMIT ?", params)
    else:
        rows = db.q(
            "SELECT id,url,ats FROM jobs "
            "WHERE (description IS NULL OR length(description) < 200) "
            "AND status IN ('new','scored','queued') ORDER BY id DESC LIMIT ?", (limit,))
    n = 0
    for r in rows:
        try:
            raw = S.fetch_single(r["url"])
        except Exception:
            continue
        if raw and raw.description and len(raw.description) > 200:
            db.run("UPDATE jobs SET description=? WHERE id=?", (raw.description, r["id"]))
            n += 1
    if n:
        con.print(f"[dim]fetched {n} full job description(s)[/dim]")
    return n


# --------------------------------------------------------------------------- #
# rank
# --------------------------------------------------------------------------- #
def rank_jobs(settings: Config, profile: Config, *, enrich: bool = True,
              console=None) -> list[dict]:
    from .score.gates import check_gates, score_job

    con = _console(console)
    db = get_db()

    if enrich:
        # Gate on real text, never on an empty feed row.
        enrich_descriptions(settings, console=con)

    # 'queued' is included deliberately: a re-rank after enrichment must be able
    # to re-evaluate jobs it queued on thinner data, otherwise they disappear from
    # the queue on the second pass.
    rows = db.q("SELECT * FROM jobs WHERE status IN ('new','scored','queued') "
                "ORDER BY id DESC LIMIT 4000")
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
            thin = len(job.get("description") or "") < 200
            out.append({**job, "score": sc.value,
                        "why": sc.why + (" [UNVERIFIED: no description]" if thin else ""),
                        "description_missing": thin})

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
    from .apply.accounts import AccountStore, MailVerifier
    from .apply.adapters import for_ats
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
        adapter = for_ats(ats)
        con.print(f"[dim]ATS: {ats} (adapter: {adapter.name})[/dim]")

        # Workday and friends run a separate tenant per employer, each needing its
        # own account. Create it once; the persistent browser profile carries the
        # session forward on every later application to the same company.
        if adapter.per_tenant_accounts and settings.get("apply.accounts.auto_create", True):
            store = AccountStore(db, settings, profile)
            creds = store.get(target)
            if creds is None:
                creds = store.create(target, ats=ats)
                con.print(f"[dim]created account for {creds.domain} "
                          f"(password in OS keychain)[/dim]")
                if settings.get("apply.accounts.email_verification.enabled", True):
                    link = MailVerifier(settings, profile).wait_for_link(
                        sender_domain=creds.domain)
                    if link:
                        sess.goto(link)
                        store.mark_verified(target)
                        con.print("[dim]email verification link followed[/dim]")
                    else:
                        con.print("[yellow]no verification email found; "
                                  "the account may need manual confirmation[/yellow]")

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

            # resume upload, using the platform's known selectors first
            if resume_path:
                for sel in adapter.resume_inputs or ['input[type=file]']:
                    if sess.upload(sel, resume_path):
                        break
            if cover_path and adapter.cover_inputs:
                for sel in adapter.cover_inputs:
                    if sel.startswith('input[type=file]') and sess.upload(sel, cover_path):
                        break

            shot = sess.screenshot(artifact / f"step-{step}.png")
            if shot:
                shots.append(str(shot))

            selector, is_submit = filler.find_advance(page)
            if not selector and adapter.advance_selectors:
                # Fall back to the platform's known controls when text matching
                # fails (Workday labels buttons by data-automation-id, not text).
                for cand in adapter.advance_selectors:
                    try:
                        if page.query_selector(cand):
                            selector, is_submit = cand, False
                            break
                    except Exception:
                        continue
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
        eligible = [j for j in queued if j["score"] >= threshold]

        # A posting with no description was never actually gated: the citizenship,
        # clearance and "we do not sponsor" checks all read the description text.
        # Applying to one blind is exactly the wasted application this system
        # exists to prevent, so it waits for enrichment instead.
        ungated = [j for j in eligible if j.get("description_missing")]
        if ungated:
            con.print(f"[yellow]{len(ungated)} job(s) held back — no job description, "
                      f"so eligibility gates could not run.[/yellow]")
        batch = [j for j in eligible if not j.get("description_missing")][:per_hour]
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
