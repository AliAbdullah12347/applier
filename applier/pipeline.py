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
from typing import Any, Callable

from .config import Config, data_path, project_paths
from .db import Database, get_db, now

# --------------------------------------------------------------------------- #


def _console(c=None):
    if c is not None:
        return c
    from rich.console import Console
    return Console()


def _rel_artifact(path) -> str:
    """A path relative to the artifacts root, forward-slashed.

    Artifact paths are stored relative and never absolute. Two reasons: the
    GUI resolves what it is handed against the artifacts root and refuses
    anything that escapes it, so an absolute path is simply unservable; and an
    absolute path bakes one machine's layout — and one person's Windows
    username — into rows that outlive the machine.
    """
    p = Path(path)
    root = project_paths()["artifacts"].resolve()
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


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

    from . import roles

    mode = roles.MODES[roles.current(settings)]
    wrong_kind = 0

    for r in rows:
        job = dict(r)
        # A posting of the wrong kind is not rejected, it is simply not for
        # this search: it stays in the table at its own score so that
        # switching mode brings it back without re-discovering anything.
        if not roles.in_scope(job, settings):
            wrong_kind += 1
            db.run("UPDATE jobs SET status='out_of_scope' WHERE id=? "
                   "AND status<>'applied'", (job["id"],))
            continue
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
    if wrong_kind:
        con.print(f"[dim]{wrong_kind} set aside as not {mode.label.lower()} "
                  f"- `applier mode` switches[/dim]")
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
    artifact = data_path(settings.get("apply.artifact_dir", "applications")) / f"{stamp}__{safe.strip()}"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "job.json").write_text(json.dumps(job, indent=2, default=str), encoding="utf-8")

    router = Router(settings, profile, db)
    bank = AnswerBank(db, profile, settings)

    # --- tailored documents ------------------------------------------------ #
    resume_path = cover_path = None
    # Imported outside the try: the handler raises TailorError, and if the
    # import itself were the thing that failed the handler would raise
    # NameError instead and bury the real cause.
    from .tailor.pipeline import TailorError
    try:
        from .tailor.pipeline import build_documents
        resume_path, cover_path = build_documents(job, settings, profile, router, artifact)
        con.print(f"[green]tailored[/green] resume -> {resume_path.name if resume_path else 'n/a'}")
    except Exception as e:
        # Resolved against the data directory, not the working directory: a
        # packaged app is launched from wherever its shortcut points.
        base = profile.get("links.resume_pdf", None)
        candidate = data_path(base) if base else None
        if candidate and candidate.exists():
            resume_path = candidate
            con.print(f"[yellow]tailoring unavailable ({e}); "
                      f"falling back to {candidate.name}[/yellow]")
        else:
            # Nothing to attach. Sending an application without a resume is
            # worse than not sending it, and this used to be a yellow line
            # that scrolled past: the run continued, the form was filled, and
            # the one field that matters was left empty.
            raise TailorError(
                f"tailoring failed ({e}) and there is no fallback resume at "
                f"{candidate or 'links.resume_pdf (unset)'}. Set links.resume_pdf "
                f"to a PDF that exists, or fix the tailoring error above — an "
                f"application with no resume attached is worse than none."
            ) from e

    # Reaching out belongs to applying, not to a separate chore later, so it
    # happens in every mode -- including the one that never opens a browser.
    if settings.get("outreach.auto_for_applications", True):
        _outreach_for_application(job, settings, profile, db, router, artifact, con)

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

        # The posting and the form are the same page on an ATS and different
        # pages on a company's own site. Find the form before filling.
        if not _enter_application(sess, page, con):
            shot = sess.screenshot(artifact / "no-form.png")
            db.run("UPDATE applications SET status='no_form', failure_reason=?, "
                   "artifact_dir=? WHERE job_id=?",
                   ("no application form found on the page or behind an apply link",
                    _rel_artifact(artifact), job_id))
            return (f"No application form found at {target}.\n"
                    f"  The page has no fields and no apply link that leads to any. "
                    f"It may need a login, or the form may be in an iframe.\n"
                    f"  screenshot: {shot}")

        filler = UniversalFiller(bank, router, settings, profile)
        max_pages = int(settings.get("apply.universal_filler.max_pages_per_application", 12))
        submitted = False
        all_filled: dict[str, Any] = {}
        # The selector->value map above is what the browser needed. This is the
        # record a human can audit: question, answer, and where the answer came
        # from. integrity.log_every_submitted_answer means this one, not that one.
        audit_trail: list[dict[str, Any]] = []
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
            audit_trail.extend(res.audit)
            if res.mismatches:
                con.print(f"[yellow]{len(res.mismatches)} field(s) did not take[/yellow]")

            # resume upload, using the platform's known selectors first.
            #
            # Recorded in the audit trail like any other answer. It was not,
            # and the omission was invisible in exactly the wrong way: the
            # upload worked, the file reached the employer, and the record of
            # what had been sent said nothing about it. integrity.yaml calls
            # for logging every submitted answer, and a resume is the most
            # consequential one on the form.
            if resume_path:
                for sel in adapter.resume_inputs or ['input[type=file]']:
                    if sess.upload(sel, resume_path):
                        audit_trail.append({
                            "question": "Resume", "answer": resume_path.name,
                            "source": "tailored", "selector": sel, "legal": False,
                            "bytes": resume_path.stat().st_size,
                        })
                        break
            if cover_path and adapter.cover_inputs:
                for sel in adapter.cover_inputs:
                    if sel.startswith('input[type=file]') and sess.upload(sel, cover_path):
                        audit_trail.append({
                            "question": "Cover letter", "answer": cover_path.name,
                            "source": "generated", "selector": sel, "legal": False,
                        })
                        break

            shot = sess.screenshot(artifact / f"step-{step}.png")
            if shot:
                shots.append(_rel_artifact(shot))

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
                           "answers_json=?, screenshots=? WHERE job_id=?",
                           (_rel_artifact(artifact), json.dumps(audit_trail),
                            json.dumps(shots), job_id))
                    return ("Form is filled and ready. Review the open browser and click "
                            "submit yourself.")
                sess.click(selector)
                submitted = True
                # Written here, not thirty lines further down, because
                # everything between is housekeeping -- settling the page,
                # the final screenshot, serialising the audit trail -- and
                # any of it can throw. When it did, the except clause below
                # marked this row 'failed', erasing the fact that an employer
                # had already received the application, and the next run
                # applied again. A double submission is the worst thing this
                # system can do, and it would happen exactly when something
                # had already gone wrong, which is when nobody is looking.
                _record_submission(db, job, settings)
                break
            if not sess.click(selector):
                break

        sess.settle(1500)
        final_shot = sess.screenshot(artifact / "final.png")
        if final_shot:
            shots.append(_rel_artifact(final_shot))

        # Attach the artifacts. COALESCE and the status guard keep this from
        # walking back the submission recorded at the moment of the click.
        db.run(
            "UPDATE applications SET status=CASE WHEN status='submitted' "
            "  THEN 'submitted' ELSE ? END, "
            "submitted_at=COALESCE(submitted_at, ?), artifact_dir=?, "
            "answers_json=?, screenshots=?, resume_path=? WHERE job_id=?",
            ("submitted" if submitted else "ready", now() if submitted else None,
             _rel_artifact(artifact), json.dumps(audit_trail), json.dumps(shots),
             str(resume_path) if resume_path else None, job_id),
        )

        if submitted:
            outcome = "Submitted"
        elif audit_trail:
            outcome = f"Filled {len(audit_trail)} field(s), not submitted"
        else:
            # Reaching the end having answered nothing is a failure wearing a
            # success message, which is how an empty run looked for a while.
            outcome = "Nothing was filled — no answerable fields were found"
        return (f"{outcome}: {job['company']} — {job['title']}"
                f"\n  artifacts: {artifact}")

    except Exception as e:
        # Never downgrade a submitted application. Something failing after the
        # employer has the form is a housekeeping failure, not an unsent
        # application, and recording it as the latter invites a second one.
        db.run("UPDATE applications SET status='failed', failure_reason=? "
               "WHERE job_id=? AND status<>'submitted'",
               (str(e)[:400], job_id))
        db.log("apply_failed", str(e)[:200], job_id=job_id, level="error")
        raise
    finally:
        if settings.get("apply.mode") != "review":
            sess.close()


# A posting URL and an application form are the same page on Greenhouse,
# Lever and Ashby, which is why this was never needed. On a company's own
# careers site they are not: the posting is prose with an "Apply" button, and
# landing there means finding no fields, filling nothing, and reporting
# "Filled but not submitted" -- a success message for having done nothing.
APPLY_LINK_SELECTORS = (
    "a#apply-link", "a[href*='apply' i]", "button[id*='apply' i]",
    "a[id*='apply' i]", "a[class*='apply' i]", "button[class*='apply' i]",
    "a:has-text('Apply')", "button:has-text('Apply')",
    "a:has-text('Submit application')", "a:has-text('Start application')",
)

# Inputs that exist on pages that are not application forms: a site search, a
# newsletter signup, a cookie banner's hidden state.
_NOT_A_FORM_FIELD = (
    "[type=hidden]", "[type=search]", "[type=submit]", "[type=button]",
    "[type=image]", "[type=reset]",
)


def _fillable_count(page) -> int:
    """How many fields on this page look like an application asking something."""
    try:
        selector = ", ".join(
            f"{tag}:not({'):not('.join(_NOT_A_FORM_FIELD)})"
            for tag in ("input", "textarea", "select"))
        return len(page.query_selector_all(selector))
    except Exception:                                   # noqa: BLE001
        return 0


def _enter_application(sess, page, con, *, hops: int = 2) -> bool:
    """Walk from a posting to its form. True when a form is in front of us.

    Deliberately conservative about what counts as a form -- two fields, not
    one -- because a newsletter box is one field and following nothing is
    better than filling a mailing list signup with a home address.
    """
    for hop in range(hops + 1):
        found = _fillable_count(page)
        if found >= 2:
            if hop:
                con.print(f"[dim]entered the application form ({found} fields)[/dim]")
            return True
        if hop == hops:
            break
        clicked = False
        for selector in APPLY_LINK_SELECTORS:
            try:
                element = page.query_selector(selector)
            except Exception:                           # noqa: BLE001
                continue
            if not element:
                continue
            try:
                element.click()
                sess.settle()
                clicked = True
                break
            except Exception:                           # noqa: BLE001
                continue
        if not clicked:
            break
    return _fillable_count(page) >= 2


def _outreach_for_application(job: dict, settings: Config, profile: Config,
                              db: Database, router, artifact: Path, con) -> int:
    """Find who to write to about this posting, and write to them.

    Applying and reaching out are one action in practice -- the referral is
    worth more than the application and the moment to send it is now, while
    the posting is open and the resume that matches it exists. Doing it as a
    separate chore later means not doing it.

    Nothing is sent. What this produces is a name, an address and a finished
    message sitting on the application, so that sending it is a copy and a
    click rather than an evening's work.

    Never raises. Outreach is an enhancement to an application; a company
    with no public team page should cost a thinner result, not a failed run.
    """
    from .outreach.discover import ContactStore
    from .outreach.draft import (NoChannelError, NoHookError, draft_message,
                                 store_draft)
    from .outreach.forjob import people_for_job

    try:
        limit = int(settings.get("outreach.max_per_application", 3))
        people = people_for_job(job, settings, db, limit=max(1, limit) * 2)
        if not people:
            return 0

        store = ContactStore(db, settings)
        written = 0
        blocked: list[str] = []
        for person in people:
            if written >= limit:
                break
            store.upsert(person)
            ok, why = store.can_contact(person)
            if not ok:
                blocked.append(f"{person.name}: {why}")
                continue
            try:
                draft = draft_message(person, profile, settings, router, job=job)
            except (NoHookError, NoChannelError) as exc:
                blocked.append(str(exc))
                continue
            except Exception as exc:                    # noqa: BLE001
                blocked.append(f"{person.name}: {exc}")
                continue
            store_draft(db, draft, job_id=int(job["id"]))
            written += 1

        if written:
            path = artifact / "outreach.md"
            rows = db.q(
                "SELECT name, role, draft_channel, draft_target, draft_subject, "
                "draft_body, hook_source_url FROM contacts WHERE job_id=? "
                "AND draft_body IS NOT NULL", (int(job["id"]),))
            parts = [f"# Outreach — {job['company']}\n",
                     "\nNothing here has been sent. Copy one and send it yourself.\n"]
            for r in rows:
                parts.append(
                    f"\n\n## {r['name']} — {r['role'] or 'role unknown'}\n\n"
                    f"**{r['draft_channel']}:** {r['draft_target']}\n\n"
                    f"**Subject:** {r['draft_subject']}\n\n"
                    f"{r['draft_body']}\n\n"
                    f"_why them: {r['hook_source_url']}_\n")
            path.write_text("".join(parts), encoding="utf-8")
            con.print(f"[green]outreach[/green] {written} draft(s) ready — "
                      f"Outreach tab, or {path.name}")
        elif blocked:
            con.print(f"[dim]outreach: nobody contactable ({blocked[0][:70]})[/dim]")
        return written
    except Exception as exc:                            # noqa: BLE001
        con.print(f"[dim]outreach unavailable ({exc})[/dim]")
        return 0


def _record_submission(db: Database, job: dict, settings: Config) -> None:
    """Mark an application sent, durably, the instant it is sent.

    Four writes that must not be separated from the click by anything that
    can fail. Each is individually idempotent, so a retry of this function
    cannot double-count a cap.
    """
    job_id = job["id"]
    db.run("UPDATE applications SET status='submitted', "
           "submitted_at=COALESCE(submitted_at, ?) WHERE job_id=?", (now(), job_id))
    db.run("UPDATE jobs SET status='applied' WHERE id=?", (job_id,))
    _bump_cap(db, job, settings)
    db.log("applied", f"{job['company']} — {job['title']}", job_id=job_id)


def _bump_cap(db: Database, job: dict, settings: Config | None = None) -> None:
    """Record one application against the employer's cap.

    Keyed the same way check_caps looks it up, or the gate would read a row
    this never wrote: an ATS-hosted posting counted against
    "job-boards.greenhouse.io" while the cap was checked against "optiver.com".
    """
    from .score.gates import cap_key_for

    caps = (settings.get("gates.application_caps", {}) or {}) if settings else {}
    dom = (cap_key_for(job, caps) or job.get("company_domain") or "").lower()
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
                    once: bool = False, console=None,
                    should_stop: Callable[[], bool] | None = None) -> None:
    """Discover, rank, apply, repeat.

    `should_stop` lets a caller that is not a terminal — the GUI — end the loop
    cleanly. It is polled only at safe points: between cycles, between
    applications, and while sleeping. Never during an application, because a
    submission is a network side effect and abandoning one halfway would leave
    a row saying "preparing" for something the employer has already received.
    """
    con = _console(console)
    db = get_db()
    stop = should_stop or (lambda: False)

    def nap(seconds: float) -> bool:
        """Sleep in slices so a stop is noticed in a second, not an hour."""
        end = time.time() + seconds
        while time.time() < end:
            if stop():
                return True
            time.sleep(min(1.0, end - time.time()))
        return stop()
    cap = max_apps or int(settings.get("apply.max_per_day", 25))
    per_hour = int(settings.get("apply.max_per_hour", 6))
    interval = int(settings.get("discovery.poll_interval_minutes", 30)) * 60
    fail_limit = int(settings.get("schedule.pause_on_consecutive_failures", 3))

    done = 0
    failures = 0
    con.print(f"[bold green]autonomous mode[/bold green] — up to {cap} applications, "
              f"{per_hour}/hour. Ctrl-C to stop.")

    while True:
        if stop():
            con.print("[yellow]stopped.[/yellow]")
            return
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
            if stop():
                con.print("[yellow]stopped between applications.[/yellow]")
                return
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
            if nap(max(5, 3600 // max(1, per_hour))):
                con.print("[yellow]stopped.[/yellow]")
                return

        if once:
            return
        con.print(f"[dim]sleeping {interval // 60} min[/dim]")
        if nap(interval):
            con.print("[yellow]stopped.[/yellow]")
            return
