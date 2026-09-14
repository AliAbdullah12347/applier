"""Drafting outreach messages.

One rule governs this module:

    **A draft with no verifiable hook source is never produced.**

Every message must cite something real and specific about *this* person —
a repository they commit to, a paper they wrote, a talk they gave, a role listed
on their employer's own page — carried as `hook_source_url`. If that field is
empty the drafter refuses. Not "warns and continues": refuses.

That single constraint is the whole difference between outreach and spam. A
message that cannot name a concrete reason for reaching this particular person
*is* a mass mail, whatever its word count, and sending it attaches the sender's
real name to it. The system is built so it cannot generate one.

Nothing is sent. The output is a person, a channel, a subject and a body, ready
for you to read, edit, and send yourself.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database, now
from ..tailor.context import candidate_brief
from .discover import Person, channel_for


class NoHookError(RuntimeError):
    """Raised when there is no citable reason to contact this person."""


@dataclass
class Draft:
    person: Person
    channel: str
    target: str
    subject: str
    body: str
    hook_fact: str
    hook_source_url: str
    ask: str

    def render(self) -> str:
        return (
            f"TO      {self.person.name}"
            + (f" — {self.person.role}" if self.person.role else "")
            + (f" @ {self.person.company}" if self.person.company else "") + "\n"
            f"VIA     {self.channel}: {self.target}\n"
            f"HOOK    {self.hook_fact}\n"
            f"SOURCE  {self.hook_source_url}\n"
            f"ASK     {self.ask}\n"
            f"{'-' * 70}\n"
            f"Subject: {self.subject}\n\n{self.body}\n"
        )


# --------------------------------------------------------------------------- #
SYSTEM = """You draft short, personal outreach messages for a student seeking an internship
referral or a brief conversation. You are writing as the candidate, in first person.

WHAT MAKES THESE WORK:
- Open with the specific thing about THIS person or their work. Not "I came across your
  profile" -- name the actual repository, paper, talk or role. You will be given it.
- One sentence establishing why the candidate is worth a reply: a concrete technical
  result, not an adjective.
- A small, specific ask. A 15-minute conversation converts far better than "can you
  refer me". Never ask for a referral in a first message.
- Sign off plainly.

HARD RULES:
- 120 words maximum for the body. Shorter is better. Busy people reply to short mail.
- Never invent anything about the candidate. Use only the brief provided.
- Never invent anything about the recipient. Use only the hook provided.
- No flattery, no "I hope this finds you well", no "I am reaching out because".
- Do not mention visa status, sponsorship or work authorisation. Not the first message.
- Plain text. No markdown, no bullet points, no tracking links, no signature block.

Return JSON: {"subject": str, "body": str, "ask": str}
where `ask` is a one-line summary of what the message actually requests."""


ASK_LADDER = [
    "a 15-minute conversation about their team's work",
    "whether they would be open to a short call",
    "a pointer to the right person for internship hiring",
]


def draft_message(person: Person, profile: Config, settings: Config, router,
                  *, purpose: str = "referral_conversation",
                  job: dict[str, Any] | None = None) -> Draft:
    """Produce one draft. Raises NoHookError when there is nothing real to cite."""
    if not person.hook_source_url or not person.hook_fact:
        raise NoHookError(
            f"{person.name}: no hook_source_url. Find something real and specific about "
            f"them first -- a repo, a paper, a talk, a team page -- or drop them from "
            f"the list. A message with no genuine reason to exist is spam."
        )

    channel, target = channel_for(person)
    if channel == "unknown":
        raise NoHookError(f"{person.name}: no usable contact channel")

    voice = profile.get("voice", {})
    brief = candidate_brief(profile, include_contact=False)

    # The strongest single line the candidate owns, used as the credibility beat.
    highlight = _best_highlight(settings)

    user = (
        f"## THE CANDIDATE\n{brief}\n\n"
        f"Strongest technical result to lead with: {highlight}\n"
        f"Portfolio: {profile.get('identity.website', '')}\n\n"
        f"## THE RECIPIENT\n"
        f"Name: {person.name}\n"
        f"Role: {person.role or 'unknown'}\n"
        f"Company: {person.company or person.domain}\n"
        f"VERIFIED HOOK (the only thing you may claim about them): {person.hook_fact}\n"
        f"Source: {person.hook_source_url}\n"
        + (f"Their bio: {person.notes}\n" if person.notes else "")
        + (f"\n## RELEVANT ROLE\n{job.get('title')} at {job.get('company')}\n"
           if job else "")
        + f"\n## ASK\n{ASK_LADDER[0]}\n"
        f"\n## VOICE\n{voice.get('tone', 'direct and concrete')}\n"
        f"Never use: {', '.join(voice.get('avoid_phrases', []))}\n"
        f"Channel: {channel} (keep it appropriate to the medium)"
    )

    data = router.json(SYSTEM, user, task="outreach_draft")
    body = _clean(str(data.get("body", "")).strip())
    subject = str(data.get("subject", "")).strip() or f"Quick question about {person.company}"

    return Draft(
        person=person, channel=channel, target=target,
        subject=subject, body=body,
        hook_fact=person.hook_fact, hook_source_url=person.hook_source_url,
        ask=str(data.get("ask", ASK_LADDER[0])),
    )


def _best_highlight(settings: Config) -> str:
    """Pull the single most compelling line from the content bank."""
    try:
        from ..tailor.bank import Bank
        bank = Bank(Path(settings.get("tailor.bank_path", "config/bank")))
        for atom in bank.atoms:
            if "default" in atom.lead_for or "ai_ml" in atom.lead_for:
                text, _ = bank.resolve(atom.text("medium"), strict=False)
                if text:
                    return text
    except Exception:
        pass
    return ""


def _clean(body: str) -> str:
    """Strip anything that makes a personal note look automated."""
    for junk in ("Best regards,", "Kind regards,", "Sincerely,", "[Your Name]",
                 "Dear Sir or Madam", "To Whom It May Concern"):
        body = body.replace(junk, "")
    lines = [ln.rstrip() for ln in body.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------- #
def follow_up(draft: Draft, profile: Config, router) -> Draft:
    """Exactly one follow-up, then stop permanently.

    Research is consistent that a single well-timed nudge lifts reply rates and a
    second one lowers them. The store enforces the limit; this just writes it.
    """
    system = (
        "Write a one-paragraph follow-up to an unanswered message. Maximum 45 words. "
        "Reference the original briefly, add one new concrete detail, and make it "
        "trivially easy to decline. No guilt, no 'just bumping this', no re-pitch. "
        'Return JSON: {"subject": str, "body": str}'
    )
    user = (f"Original subject: {draft.subject}\n\nOriginal body:\n{draft.body}\n\n"
            f"Recipient: {draft.person.name} at {draft.person.company}")
    data = router.json(system, user, task="outreach_draft")
    return Draft(
        person=draft.person, channel=draft.channel, target=draft.target,
        subject=str(data.get("subject") or f"Re: {draft.subject}"),
        body=_clean(str(data.get("body", ""))),
        hook_fact=draft.hook_fact, hook_source_url=draft.hook_source_url,
        ask="follow-up (final)",
    )


def save_batch(drafts: list[Draft], out_dir: Path, db: Database) -> Path:
    """Write a review file you can read top to bottom in one sitting."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"outreach-{now().replace(':', '')[:15]}.md"
    parts = [
        "# Outreach drafts\n",
        f"_{len(drafts)} message(s). Nothing has been sent._\n",
        "\nRead, edit, send yourself. Every message cites a real source — if one "
        "reads as generic, the hook was weak and the person should be dropped rather "
        "than the message padded.\n",
    ]
    for i, d in enumerate(drafts, 1):
        parts.append(f"\n\n## {i}. {d.person.name}\n\n```\n{d.render()}```\n")
    path.write_text("".join(parts), encoding="utf-8")
    for d in drafts:
        db.run("UPDATE contacts SET stage='drafted' WHERE person_key=?", (d.person.key,))
    db.log("outreach_drafted", f"{len(drafts)} drafts", path=str(path))
    return path
