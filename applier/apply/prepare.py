"""Preparing an application for a page this program cannot reach itself.

Handshake sits behind a university single sign-on. So does Workday at most
employers, and so does anything that wanted an account before it would show
you a form. `applier apply <url>` drives its own browser, and that browser
is nobody: it has no session, so it sees a login page and stops.

The browser that *is* signed in is the one already open. This prepares
everything that application needs -- a tailored resume, a cover letter, and
an answer for every field on the form -- and hands it back, so an extension
can fill the page the person is already looking at.

What this is not
----------------
It does not submit, and it holds no browser. The decision to send stays
where the session is, which is also the only place it legally belongs: this
is a form in a person's own authenticated session, filled in front of them.

Why it does not reimplement anything
------------------------------------
The answers come from `UniversalFiller.resolve_all` and `llm_fill`, the same
two calls the pipeline makes. Only `_set` ever touched a page, so the rest
was always page-independent and nobody had noticed. Two ideas of "what is
the answer to this question" is how the extension and the CLI end up putting
different things on the same employer's form.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from ..config import Config

# A form with more fields than this is not a form, it is a page that has been
# misread -- and every extra field is a model call.
MAX_FIELDS = 60

# Answers below this are offered but marked, so the extension can show them
# differently rather than typing a guess into an employer's form.
DEFAULT_CONFIDENCE = 0.80


def _field(raw: dict) -> Any:
    from .universal import Field

    ml = raw.get("maxlength")
    return Field(
        selector=str(raw.get("selector") or ""),
        tag=str(raw.get("tag") or "input").lower(),
        type=str(raw.get("type") or "text").lower(),
        label=str(raw.get("label") or "")[:400],
        required=bool(raw.get("required")),
        options=[str(o) for o in (raw.get("options") or [])][:60],
        value=str(raw.get("value") or ""),
        placeholder=str(raw.get("placeholder") or ""),
        maxlength=int(ml) if str(ml).isdigit() else None,
    )


def prepare(job: dict, fields: list[dict], settings: Config, profile: Config,
            db, *, artifact_root: Path | None = None) -> dict[str, Any]:
    """Documents and answers for one posting, with nothing sent.

    `job` carries what the page said about itself -- url, title, company,
    description -- because the server cannot fetch an authenticated page to
    find out. That text is an employer's, so it goes through the same
    sanitiser every other posting does before it reaches a model.
    """
    from ..llm import Router
    from ..score.gates import check_gates, score_job
    from ..tailor.context import sanitize_job_text
    from ..tailor.pipeline import build_cover_letter, build_resume, document_name
    from .answers import AnswerBank
    from .universal import UniversalFiller

    row = {
        "fingerprint": f"ext:{job.get('url')}",
        "weak_key": f"{job.get('company','')}|{job.get('title','')}".lower(),
        "source": "extension",
        "company": str(job.get("company") or "")[:200],
        "title": str(job.get("title") or "")[:300],
        "url": str(job.get("url") or ""),
        "location": str(job.get("location") or "")[:200],
        "description": sanitize_job_text(str(job.get("description") or ""))[:40000],
    }
    job_id = db.upsert_job(row)
    stored = dict(db.one("SELECT * FROM jobs WHERE id=?", (job_id,)))

    # The eligibility gate runs here exactly as it does for a posting this
    # program found itself. A role he cannot lawfully take is still refused,
    # and refused before any document is written for it.
    gate = check_gates(stored, settings, profile, db)
    if not gate.passed:
        db.run("UPDATE jobs SET gate_status='rejected', gate_reason=? WHERE id=?",
               (gate.reason, job_id))
        return {"job_id": job_id, "ok": False, "reason": gate.reason,
                "company": stored["company"], "title": stored["title"]}
    score = score_job(stored, settings, profile, gate)

    root = artifact_root or Path(str(settings.get("paths.artifacts", "applications")))
    artifact = Path(root) / f"handshake-{job_id}"
    artifact.mkdir(parents=True, exist_ok=True)

    router = Router(settings, profile, db)
    resume_path, report, _sel = build_resume(stored, settings, profile, artifact)
    cover_path = None
    try:
        cover_path = build_cover_letter(stored, settings, profile, router,
                                        artifact, sel=_sel)
    except Exception:                                   # noqa: BLE001
        # A cover letter is worth having and is not worth failing over; a
        # Handshake application that wants one will say so in a field, and
        # that field is answered either way.
        cover_path = None

    filler = UniversalFiller(AnswerBank(db, profile, settings), router,
                             settings, profile)
    parsed = [_field(f) for f in fields[:MAX_FIELDS] if f.get("selector")]
    resolved, needs_llm = filler.resolve_all(parsed, stored)
    if needs_llm:
        subjective = [f for f in needs_llm if f.is_free_text or f.required]
        if subjective:
            resolved.update(filler.llm_fill(subjective, stored))

    threshold = float(settings.get("apply.universal_filler.confidence_threshold",
                                   DEFAULT_CONFIDENCE))
    answers, unanswered = [], []
    by_sel = {f.selector: f for f in parsed}
    for sel, r in resolved.items():
        f = by_sel.get(sel)
        if f is None or r.value in (None, ""):
            continue
        value = str(r.value)
        if r.needs_qualifier and r.qualifier_text and f.is_free_text:
            value = f"{value}. {r.qualifier_text}"
        answers.append({
            "selector": sel, "value": value, "label": f.question,
            "source": r.source, "confidence": round(float(r.confidence), 2),
            # Below the threshold it is a suggestion, not an answer. The
            # extension shows it and does not type it: a guess on a legal
            # question is the one thing worth a person's attention.
            "certain": not (r.confidence < threshold and r.source == "llm"),
            "legal": bool(r.needs_qualifier or r.source == "profile_legal"),
        })
    answered = {a["selector"] for a in answers if a["certain"]}
    for f in parsed:
        if f.selector not in answered and f.required and f.type != "file":
            unanswered.append({"selector": f.selector, "label": f.question,
                               "type": f.type, "options": f.options})

    return {
        "ok": True,
        "job_id": job_id,
        "company": stored["company"],
        "title": stored["title"],
        "score": round(float(score.value), 2),
        "why": score.why,
        "sponsorship": gate.sponsorship,
        "resume": _document(resume_path, document_name(profile, stored, "Resume", ".pdf")),
        "cover_letter": (
            {"filename": document_name(profile, stored, "Cover Letter", ".txt"),
             "text": Path(cover_path).read_text(encoding="utf-8", errors="replace")}
            if cover_path and Path(cover_path).is_file() else None),
        "one_page": bool(getattr(report, "ok", False)),
        "answers": answers,
        "unanswered": unanswered,
        "artifact_dir": str(artifact),
    }


def _document(path: Path | None, filename: str) -> dict[str, Any] | None:
    """A file the extension can attach, as bytes it can rebuild.

    A content script cannot read a local path -- that is the whole point of
    the file-input sandbox -- so the PDF travels as base64 and is rebuilt
    into a File on the page. It never leaves the machine: the only endpoint
    this extension knows is 127.0.0.1.
    """
    if not path or not Path(path).is_file():
        return None
    data = Path(path).read_bytes()
    return {"filename": filename, "bytes": len(data),
            "content_type": "application/pdf",
            "b64": base64.b64encode(data).decode("ascii")}
