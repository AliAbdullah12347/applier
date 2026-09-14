"""Account creation and credential storage.

Many employers make you register before you can apply. This module creates that
account, generates a strong unique password, stores it in the OS keychain
(Windows Credential Manager / macOS Keychain / Secret Service), and — where the
site sends a confirmation link — watches the inbox and clicks it.

Credentials are NEVER written to the database, to YAML, or to logs. The database
stores only a keychain *reference*. If someone steals applier.db they get the
usernames and nothing else.
"""

from __future__ import annotations

import email
import imaplib
import re
import secrets
import string
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from ..config import Config, get_secret, set_secret
from ..db import Database, now

KEYRING_SERVICE = "applier-sites"

# Deliberately excludes characters that break naive site-side validators.
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*-_=+"

VERIFY_LINK_RE = re.compile(
    r"https?://[^\s\"'<>]{10,300}?(?:verify|confirm|activate|validate)[^\s\"'<>]{0,200}",
    re.I,
)


@dataclass
class Credentials:
    domain: str
    username: str
    password: str
    created: bool = False


def generate_password(length: int = 24) -> str:
    """Strong, and guaranteed to satisfy the usual four-class policy."""
    while True:
        pw = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*-_=+" for c in pw)):
            return pw


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


class AccountStore:
    def __init__(self, db: Database, settings: Config, profile: Config) -> None:
        self.db = db
        self.s = settings
        self.p = profile

    # ------------------------------------------------------------------ #
    def username(self) -> str:
        """Prefer the personal address: institutional Workspace admins can block
        third-party apps and break verification polling."""
        personal = self.p.get("identity.email_personal", None)
        if personal and str(personal).upper() != "ASK":
            return str(personal)
        return str(self.p.get("identity.email"))

    def get(self, url: str) -> Credentials | None:
        dom = domain_of(url)
        row = self.db.one("SELECT * FROM accounts WHERE domain=? ORDER BY id DESC LIMIT 1", (dom,))
        if not row:
            return None
        pw = get_secret(row["keyring_key"], service=row["keyring_service"])
        if not pw:
            return None
        return Credentials(dom, row["username"], pw)

    def create(self, url: str, *, ats: str | None = None) -> Credentials:
        """Mint and persist credentials for a site. Idempotent per domain."""
        existing = self.get(url)
        if existing:
            return existing

        dom = domain_of(url)
        user = self.username()
        pw = generate_password(int(self.s.get("apply.accounts.password_length", 24)))
        key = f"{dom}::{user}"

        set_secret(key, pw, service=KEYRING_SERVICE)
        self.db.run(
            "INSERT OR REPLACE INTO accounts"
            "(domain,ats,username,keyring_service,keyring_key,verified,created_at)"
            " VALUES(?,?,?,?,?,0,?)",
            (dom, ats, user, KEYRING_SERVICE, key, now()),
        )
        self.db.log("account_created", f"{dom} ({user})")
        return Credentials(dom, user, pw, created=True)

    def mark_verified(self, url: str) -> None:
        self.db.run("UPDATE accounts SET verified=1 WHERE domain=?", (domain_of(url),))

    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.q("SELECT * FROM accounts ORDER BY domain")]


# --------------------------------------------------------------------------- #
# email verification
# --------------------------------------------------------------------------- #
class MailVerifier:
    """Polls an inbox over IMAP for a confirmation link.

    Use an app password, stored in the keychain under APPLIER_IMAP_PASSWORD.
    Read-only: it searches and reads, it never deletes.
    """

    def __init__(self, settings: Config, profile: Config) -> None:
        self.s = settings
        self.p = profile

    def _connect(self) -> imaplib.IMAP4_SSL | None:
        user = self.p.get("identity.email_personal", None)
        if not user or str(user).upper() == "ASK":
            return None
        pw = get_secret("APPLIER_IMAP_PASSWORD")
        if not pw:
            return None
        host = self.s.get("apply.accounts.email_verification.imap_host", None)
        if not host:
            dom = str(user).split("@")[-1].lower()
            host = {
                "gmail.com": "imap.gmail.com",
                "outlook.com": "outlook.office365.com",
                "hotmail.com": "outlook.office365.com",
            }.get(dom)
        if not host:
            return None
        try:
            m = imaplib.IMAP4_SSL(host)
            m.login(str(user), pw)
            return m
        except Exception:
            return None

    def wait_for_link(self, *, sender_domain: str, timeout: int | None = None,
                      poll: int | None = None) -> str | None:
        """Return the first verification URL from `sender_domain`, or None."""
        cfg = self.s.get("apply.accounts.email_verification", {})
        timeout = timeout or int(cfg.get("timeout_seconds", 300))
        poll = poll or int(cfg.get("poll_seconds", 15))

        mail = self._connect()
        if mail is None:
            return None

        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                try:
                    mail.select("INBOX")
                    typ, data = mail.search(None, "UNSEEN")
                    if typ == "OK":
                        for num in reversed((data[0] or b"").split()):
                            typ, msg_data = mail.fetch(num, "(RFC822)")
                            if typ != "OK" or not msg_data or not msg_data[0]:
                                continue
                            msg = email.message_from_bytes(msg_data[0][1])
                            frm = str(msg.get("From", "")).lower()
                            if sender_domain.lower() not in frm:
                                continue
                            link = self._extract_link(msg)
                            if link:
                                return link
                except Exception:
                    pass
                time.sleep(poll)
        finally:
            try:
                mail.logout()
            except Exception:
                pass
        return None

    @staticmethod
    def _extract_link(msg) -> str | None:
        bodies: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    try:
                        bodies.append(part.get_payload(decode=True).decode("utf-8", "replace"))
                    except Exception:
                        continue
        else:
            try:
                bodies.append(msg.get_payload(decode=True).decode("utf-8", "replace"))
            except Exception:
                pass
        for body in bodies:
            m = VERIFY_LINK_RE.search(body)
            if m:
                return m.group(0).rstrip(".,)>\"'")
        return None
