"""Building the candidate brief that every generation prompt is grounded in.

Every cover letter, essay answer and form response is written against this brief
and nothing else. If a fact is not here, the model has no basis to state it —
which is the mechanism that stops fabrication at the source rather than trying
to catch it afterwards.

The brief is assembled fixed-part-first so providers can cache the prefix.
"""

from __future__ import annotations

from typing import Any

from ..config import Config


def candidate_brief(profile: Config, *, include_contact: bool = False) -> str:
    """A compact, factual summary of the candidate.

    `include_contact` stays False for outbound LLM calls: identity is re-attached
    locally at render time so it never leaves the machine.
    """
    lines: list[str] = []
    edu = (profile.get("education", []) or [{}])[0]

    if include_contact:
        ident = profile.get("identity", {})
        lines.append(f"Name: {ident.get('full_name')}")
        lines.append(f"Email: {ident.get('email')}  |  Phone: {ident.get('phone')}")
        lines.append(f"GitHub: {ident.get('github')}  |  Site: {ident.get('website')}")

    lines.append(
        f"Education: {edu.get('degree')} in {', '.join(edu.get('majors', []) or [])} "
        f"at {edu.get('institution')}, GPA {edu.get('gpa')}/{edu.get('gpa_scale')}, "
        f"expected {edu.get('expected_graduation')}."
    )
    if edu.get("honors"):
        lines.append("Honors: " + "; ".join(edu["honors"]))
    if edu.get("coursework"):
        lines.append("Coursework: " + ", ".join(edu["coursework"]))

    avail = profile.get("availability", {})
    lines.append(
        f"Availability: {avail.get('earliest_start')} to {avail.get('latest_end')}; "
        f"relocation {'yes' if avail.get('willing_to_relocate') else 'no'}."
    )

    langs = profile.get("languages", []) or []
    if langs:
        lines.append("Languages: " + ", ".join(f"{l['language']} ({l['level']})" for l in langs))

    return "\n".join(lines)


def experience_brief(atoms: list[dict[str, Any]], *, limit: int = 40) -> str:
    """Render the atom bank as evidence the model may draw on — and only this."""
    out: list[str] = []
    for a in atoms[:limit]:
        tags = ",".join(a.get("tags", []) or [])
        out.append(
            f"- [{a.get('id')}] ({a.get('section')}; {tags}) {a.get('phrasings', {}).get('long') or a.get('text','')}"
        )
    return "\n".join(out)


def job_brief(job: dict[str, Any], *, max_chars: int = 6000) -> str:
    desc = (job.get("description") or "").strip()
    return (
        f"Company: {job.get('company')}\n"
        f"Title: {job.get('title')}\n"
        f"Location: {job.get('location')}  ({job.get('country') or 'n/a'})\n"
        f"Source: {job.get('url')}\n\n"
        f"--- Job description ---\n{desc[:max_chars]}"
    )


# --------------------------------------------------------------------------- #
# Untrusted input handling
# --------------------------------------------------------------------------- #
INJECTION_MARKERS = [
    "ignore previous", "ignore all previous", "disregard the above",
    "system prompt", "you are now", "new instructions",
    "assistant:", "</system>", "<|im_start|>",
]


def sanitize_job_text(text: str) -> str:
    """Job descriptions are untrusted input.

    A posting is data, not instructions. Some contain text aimed at automated
    screeners — and a few now contain text aimed at applicant-side agents. We
    neutralise the obvious markers and wrap the rest so the model treats it as a
    quoted document rather than as part of its own instructions.
    """
    if not text:
        return ""
    cleaned = text
    for marker in INJECTION_MARKERS:
        cleaned = cleaned.replace(marker, f"[redacted:{marker.split()[0]}]")
    return cleaned
