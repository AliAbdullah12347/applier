"""Looking in your inbox for the employer's side of the story.

Everything else this program records about a submission is its own account
of itself: it clicked a button, the page afterwards looked like this, here
is a screenshot it took. None of that is independent. If the form silently
failed, the record would read exactly the same.

The acknowledgement an employer sends is the one piece of evidence that does
not come from this program. If it arrives, something was received. That is
worth more than every screenshot in the bundle, and it is the only thing
that answers "are you sure it went?" with anything but a shrug.

It does not arrive on the pipeline's schedule -- minutes to hours later, and
sometimes never, because plenty of boards send nothing at all. So this is a
pass you run over the mailbox afterwards, not a step that blocks a run.

Read-only, by construction: it searches and fetches and never deletes,
never moves, never marks anything seen.

Absence proves nothing
----------------------
No acknowledgement does not mean no application. Many employers never send
one; mail lands in spam; the address on the form may not be the mailbox
being read. So a match raises the evidence to `acknowledged` and no match
leaves it exactly where it was. Nothing here can lower anything.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config, get_secret
from .confirm import ACKNOWLEDGED, stronger

# Subjects an acknowledgement uses. Matched against subject and body, so a
# bare "Thank you for applying" subject and a longer body both hit.
ACK_PHRASES = (
    "thank you for applying", "thanks for applying", "application received",
    "we received your application", "we have received your application",
    "we've received your application", "your application has been received",
    "application submitted", "received your application",
    "thank you for your application", "thank you for your interest",
    "application confirmation", "we got your application",
)

# Mail that mentions an application but is not an acknowledgement of one.
# A rejection is proof of receipt too, but calling it an acknowledgement in
# the panel would read as good news.
NOT_ACK = (
    "unfortunately", "not moving forward", "no longer under consideration",
    "decided to move forward with other", "will not be proceeding",
)

# How far back to look. An acknowledgement that predates the submission is
# about something else -- very often a different application to the same
# company, which is exactly the kind of false match that would make this
# feature worse than useless.
WINDOW_BEFORE = timedelta(minutes=5)


@dataclass(frozen=True)
class Ack:
    job_id: int
    subject: str
    sender: str
    received: str


class MailboxError(RuntimeError):
    """Raised only on the path a person triggers and waits on."""


def _words(text: str) -> set[str]:
    """Distinctive words of a company name, for matching against mail.

    "Inc", "Technologies" and "Labs" appear in half of all employers, so a
    match on one of those is a match on nothing.
    """
    stop = {"inc", "llc", "ltd", "corp", "corporation", "company", "co",
            "technologies", "technology", "tech", "labs", "lab", "group",
            "holdings", "systems", "solutions", "services", "the", "and",
            "global", "international", "plc", "gmbh", "sa", "ag"}
    return {w for w in re.findall(r"[a-z0-9]{3,}", str(text or "").lower())
            if w not in stop}


def _body(msg) -> str:
    parts: list[str] = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        parts.append(part.get_payload(decode=True)
                                     .decode("utf-8", "replace"))
                    except Exception:                   # noqa: BLE001
                        continue
        else:
            parts.append(msg.get_payload(decode=True).decode("utf-8", "replace"))
    except Exception:                                   # noqa: BLE001
        pass
    return " ".join(parts).lower()


def _connect(settings: Config, profile: Config) -> imaplib.IMAP4_SSL:
    """The same mailbox, the same credential, as account verification uses."""
    user = str(profile.get("identity.email_personal", "") or "").strip()
    if not user or user.upper() == "ASK":
        user = str(profile.get("identity.email", "") or "").strip()
    if not user or user.upper() == "ASK":
        raise MailboxError("no mailbox address: set identity.email in your profile")

    pw = get_secret("APPLIER_IMAP_PASSWORD")
    if not pw:
        raise MailboxError(
            "no mailbox password in the keychain under APPLIER_IMAP_PASSWORD. "
            "Set it with `applier config set-key APPLIER_IMAP_PASSWORD`. For "
            "Gmail this is an App Password, not the account password.")

    host = settings.get("apply.accounts.email_verification.imap_host", None)
    if not host:
        host = {
            "gmail.com": "imap.gmail.com",
            "colgate.edu": "imap.gmail.com",
            "outlook.com": "outlook.office365.com",
            "hotmail.com": "outlook.office365.com",
        }.get(user.split("@")[-1].lower())
    if not host:
        raise MailboxError(
            f"no IMAP host known for {user.split('@')[-1]}; set "
            f"apply.accounts.email_verification.imap_host")

    try:
        box = imaplib.IMAP4_SSL(str(host))
        box.login(user, pw)
        return box
    except imaplib.IMAP4.error as e:
        raise MailboxError(f"{host} rejected the login for {user}: {e}") from e
    except OSError as e:
        raise MailboxError(f"could not reach {host}: {e}") from e


def _is_ack(subject: str, body: str) -> bool:
    blob = f"{subject} {body}".lower()
    if any(p in blob for p in NOT_ACK):
        return False
    return any(p in blob for p in ACK_PHRASES)


def _received_at(msg) -> datetime | None:
    try:
        when = email.utils.parsedate_to_datetime(msg.get("Date", ""))
    except Exception:                                   # noqa: BLE001
        return None
    if when is None:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def scan(settings: Config, profile: Config, pending: list[dict],
         *, days: int = 30, limit: int = 400) -> list[Ack]:
    """Match acknowledgement mail against applications awaiting corroboration.

    `pending` is the rows to look for: each needs `job_id`, `company` and
    `submitted_at`. Returns one Ack per row matched -- never more, so one
    mail cannot corroborate four applications to the same company.
    """
    if not pending:
        return []

    wanted = []
    for row in pending:
        when = _parse_ts(row.get("submitted_at"))
        words = _words(row.get("company"))
        if when and words:
            wanted.append((row, when - WINDOW_BEFORE, words))
    if not wanted:
        return []

    box = _connect(settings, profile)
    found: list[Ack] = []
    claimed: set[int] = set()
    try:
        box.select("INBOX", readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = box.search(None, "SINCE", since)
        if typ != "OK":
            return []
        for num in reversed((data[0] or b"").split()[-limit:]):
            # BODY.PEEK, not BODY: fetching the latter sets \Seen, and a tool
            # that silently marks your mail read is a tool you stop running.
            typ, raw = box.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                continue
            msg = email.message_from_bytes(raw[0][1])
            subject = str(email.header.make_header(
                email.header.decode_header(msg.get("Subject", "") or "")))
            body = _body(msg)
            if not _is_ack(subject, body):
                continue

            when = _received_at(msg)
            sender = str(msg.get("From", ""))
            haystack = _words(f"{subject} {sender}") | _words(body[:4000])
            for row, floor, words in wanted:
                job_id = int(row["job_id"])
                if job_id in claimed:
                    continue
                if when is not None and when < floor:
                    continue            # predates the submit: a different one
                if not (words & haystack):
                    continue
                claimed.add(job_id)
                found.append(Ack(job_id=job_id, subject=subject[:200],
                                 sender=sender[:200],
                                 received=(when or datetime.now(timezone.utc)).isoformat()))
                break
    finally:
        try:
            box.logout()
        except Exception:                               # noqa: BLE001
            pass
    return found


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def pending_rows(db) -> list[dict]:
    """Submitted applications that nothing outside this program confirms."""
    return [dict(r) for r in db.q(
        "SELECT a.job_id, a.submitted_at, a.evidence, j.company "
        "FROM applications a JOIN jobs j ON j.id = a.job_id "
        "WHERE a.status = 'submitted' "
        "  AND COALESCE(a.evidence, '') <> ? "
        "ORDER BY a.submitted_at DESC", (ACKNOWLEDGED,))]


def record(db, acks: list[Ack]) -> int:
    """Raise matched rows to `acknowledged`. Only ever upward."""
    n = 0
    for ack in acks:
        row = db.one("SELECT evidence FROM applications WHERE job_id=?", (ack.job_id,))
        if row and not stronger(row["evidence"], ACKNOWLEDGED):
            continue
        db.run(
            "UPDATE applications SET evidence=?, evidence_detail=?, "
            "acknowledged_at=? WHERE job_id=?",
            (ACKNOWLEDGED, f"{ack.sender} - {ack.subject}"[:400],
             ack.received, ack.job_id))
        n += 1
    return n
