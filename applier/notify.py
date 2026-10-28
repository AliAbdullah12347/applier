"""Telling you something happened while you were not watching.

The only thing in this program that sends anything to anyone. Outreach
drafts messages and stops; that is deliberate and stays that way. This
sends one kind of mail, to one address, about one event: an application
this software submitted in your name.

Three rules it is built around:

**It cannot fail the thing it reports on.** A submission is written to the
database the instant the click returns, because an employer already has the
form and a second submission is the worst thing this system can do. A mail
server that is slow, down, or rejecting a password is a fact about a mail
server. Every function here that runs after a submission swallows its own
errors and returns a string; none of them raise, and none of them touch the
applications table.

**The password is never in a file.** It lives in the OS keychain beside the
API keys, read through `get_secret`, named by `notify.email.password_env` so
the settings file holds the name of a variable rather than a credential.

**Employer text never reaches a header.** The company and title come from a
job posting, which is written by someone else. A newline in a subject line
is how mail headers get injected, so the subject is built from scrubbed
pieces and the posting's own text stays in the body.
"""

from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from .config import Config, get_secret

# A mail server that does not answer must not hold up the pipeline behind it.
# The submission is already recorded by the time anything here runs; this is
# the window in which the program is doing nothing else.
SMTP_TIMEOUT = 20

# Implicit TLS rather than STARTTLS. 465 is the conventional port for it.
SMTPS_PORT = 465


class NotifyError(RuntimeError):
    """Raised only by the paths a person triggers and waits on."""


def _one_line(text: Any, *, limit: int = 120) -> str:
    """Header-safe: no control characters, no newlines, bounded length.

    `EmailMessage` already refuses a header containing a newline, but it
    raises to do it, and the caller of a notifier is in no position to handle
    an exception caused by a job title. Scrubbing first means an odd posting
    produces an odd subject line rather than a failure.
    """
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))).strip()[:limit]


@dataclass(frozen=True)
class EmailConfig:
    to: str
    host: str
    port: int
    username: str
    password: str
    starttls: bool

    @property
    def implicit_tls(self) -> bool:
        return self.port == SMTPS_PORT


def email_config(settings: Config, profile: Config) -> EmailConfig | None:
    """The configured sender, or None when email is off or unusable.

    Returns None rather than raising on an incomplete configuration, because
    the common caller is a submission that has already happened. `describe`
    is the function that explains why nothing would be sent.
    """
    raw = settings.get("notify.email", None)
    # This key held a bool before it held a block. An old settings file is
    # not a configuration error, it is a configuration that predates the
    # feature, and it means off.
    if not isinstance(raw, dict) or not raw.get("enabled"):
        return None

    to = str(raw.get("to") or "").strip() or str(
        (profile.get("identity", {}) or {}).get("email") or "").strip()
    host = str(raw.get("host") or "").strip()
    if not to or not host:
        return None

    password = get_secret(str(raw.get("password_env") or "APPLIER_SMTP_PASSWORD"))
    if not password:
        return None

    return EmailConfig(
        to=to, host=host, port=int(raw.get("port") or 587),
        username=str(raw.get("username") or "").strip() or to,
        password=password, starttls=bool(raw.get("starttls", True)),
    )


def describe(settings: Config, profile: Config) -> dict[str, Any]:
    """Whether mail would be sent, and if not, the first reason why not.

    Deliberately never returns the password, nor whether a given string is
    the password -- only that one is present. Same rule as `/api/secrets`.
    """
    raw = settings.get("notify.email", None)
    block = raw if isinstance(raw, dict) else {}
    env = str(block.get("password_env") or "APPLIER_SMTP_PASSWORD")
    to = str(block.get("to") or "").strip() or str(
        (profile.get("identity", {}) or {}).get("email") or "").strip()

    reason = ""
    if not isinstance(raw, dict) or not block.get("enabled"):
        reason = "notify.email.enabled is false"
    elif not to:
        reason = ("no address: set notify.email.to, or fill identity.email "
                  "in your profile")
    elif not str(block.get("host") or "").strip():
        reason = "no notify.email.host"
    elif not get_secret(env):
        reason = (f"no password in the keychain under {env}. "
                  f"Set it with `applier config set-key {env}`.")

    return {
        "enabled": bool(block.get("enabled")),
        "ready": not reason,
        "reason": reason,
        "to": to,
        "host": str(block.get("host") or ""),
        "port": int(block.get("port") or 587),
        "username": str(block.get("username") or "").strip() or to,
        "password_env": env,
        "password_set": bool(get_secret(env)),
    }


def send(cfg: EmailConfig, subject: str, body: str) -> None:
    """One message. Raises NotifyError with something a person can act on."""
    msg = EmailMessage()
    msg["From"] = cfg.username
    msg["To"] = cfg.to
    msg["Subject"] = _one_line(subject, limit=200)
    msg["Auto-Submitted"] = "auto-generated"      # RFC 3834: do not auto-reply
    msg.set_content(body)

    try:
        if cfg.implicit_tls:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.host, cfg.port, timeout=SMTP_TIMEOUT,
                context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT)
        with server:
            if cfg.starttls and not cfg.implicit_tls:
                server.starttls(context=ssl.create_default_context())
            server.login(cfg.username, cfg.password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        # The overwhelmingly likely cause, and the one with a specific fix:
        # Gmail rejects an account password here and wants an App Password.
        raise NotifyError(
            f"{cfg.host} rejected the login for {cfg.username}. If this is "
            f"Gmail, it needs an App Password (Google Account > Security > "
            f"2-Step Verification > App passwords), not your normal one. "
            f"({e.smtp_code})") from e
    except (OSError, smtplib.SMTPException) as e:
        raise NotifyError(f"{cfg.host}:{cfg.port} -- {e}") from e


def submission_message(job: dict, *, form_url: str = "", final_url: str = "",
                       resume: str = "", resume_sha: str = "",
                       fields: int = 0) -> tuple[str, str]:
    """The subject and body for one submitted application."""
    company = _one_line(job.get("company"), limit=60) or "unknown company"
    title = _one_line(job.get("title"), limit=90) or "unknown role"
    # ASCII throughout. An em-dash in a subject forces RFC 2047 encoded-word
    # encoding for one character ("=?utf-8?b?4oCU?="), which is correct and
    # ugly everywhere it is not decoded -- logs, filters, a phone preview.
    # The body follows the same rule so the mail survives any client.
    subject = f"Applied: {company} - {title}"

    lines = ["An application was submitted on your behalf.", "",
             f"  Company   {company}",
             f"  Role      {title}"]
    location = _one_line(job.get("location"), limit=80)
    if location:
        lines.append(f"  Location  {location}")
    if form_url:
        lines.append(f"  Form      {form_url}")
    if final_url and final_url != form_url:
        lines.append(f"  Landed    {final_url}")
    lines.append(f"  Fields    {fields} answered")
    if resume:
        lines.append(f"  Resume    {resume}")
    if resume_sha:
        # Enough to match this mail against the file in the artifact bundle,
        # and against the PDF an employer has, without quoting all 64.
        lines.append(f"  Checksum  {resume_sha[:16]}...")
    lines += ["",
              "The full record -- every question and the answer given, the "
              "screenshots taken before and after filling, and the files "
              "attached -- is in the Applications tab, and in SUBMITTED.md "
              "in this application's folder."]
    return subject, "\n".join(lines)


def on_submission(settings: Config, profile: Config, job: dict, **detail: Any) -> str:
    """Notify about a submitted application. Never raises. Never retries.

    Called immediately after the applications row is written, and that row is
    the record of an application an employer already has. Nothing from here
    is allowed to change it, delay it materially, or propagate out: a mail
    server having a bad afternoon is not an unsent application, and treating
    it as one is how a job gets applied to twice.
    """
    try:
        cfg = email_config(settings, profile)
        if cfg is None:
            return "email notification not configured"
        subject, body = submission_message(job, **detail)
        send(cfg, subject, body)
        return f"emailed {cfg.to}"
    except Exception as e:                              # noqa: BLE001
        return f"email notification failed: {str(e)[:200]}"


def send_test(settings: Config, profile: Config) -> str:
    """Prove the configuration works without waiting for a real submission.

    A notifier nobody can test is a notifier nobody can trust, and at the
    levels that never submit there is no other way to see one. This path is
    triggered by a person who is waiting for the answer, so it raises.
    """
    state = describe(settings, profile)
    if not state["ready"]:
        raise NotifyError(state["reason"] or "email notification is not configured")
    cfg = email_config(settings, profile)
    if cfg is None:                                     # pragma: no cover
        raise NotifyError("email notification is not configured")

    subject, body = submission_message(
        {"company": "Example Corp", "title": "Software Engineering Intern",
         "location": "Remote"},
        form_url="https://example.com/apply",
        final_url="https://example.com/thanks",
        resume="Your Name - Example Corp - Resume.pdf",
        resume_sha="0" * 64, fields=14)
    send(cfg, "[test] " + subject,
         "This is a test. No application was submitted.\n\n" + body)
    return cfg.to
