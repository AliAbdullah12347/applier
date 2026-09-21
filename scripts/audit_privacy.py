#!/usr/bin/env python3
"""Privacy audit — prove this repository is safe to publish.

    python scripts/audit_privacy.py

Checks, in order of how badly each would hurt:

  1. SECRETS in tracked files or in git history (API keys, tokens, private keys,
     passwords). A leaked key is exploitable the moment the repo goes public,
     and rewriting history after the fact does not un-leak it.
  2. PERSONAL DATA in tracked files or history — names, emails, phone numbers,
     addresses, dates of birth, immigration status.
  3. GITIGNORE COVERAGE for every path that is supposed to stay local, checked
     by asking git itself rather than by reading the file.
  4. GENERATED ARTIFACTS — rendered resumes, cover letters, the database, the
     browser profile (which holds live session cookies).
  5. TEMPLATE HYGIENE — the *.example.* files must contain placeholders only.

History matters as much as the working tree: `git rm` leaves the old content
reachable, so anyone can read it with `git log -p`. Exit code is non-zero if
anything fails, so this can gate a push.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------- #
SECRET_PATTERNS: dict[str, str] = {
    "Google/Gemini API key": r"AIza[0-9A-Za-z_\-]{30,}",
    "Google OAuth token": r"\bAQ\.[A-Za-z0-9_\-]{20,}",
    "OpenAI key": r"\bsk-[A-Za-z0-9]{20,}",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9\-_]{20,}",
    "GitHub token": r"\bgh[pousr]_[A-Za-z0-9]{20,}",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key block": r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY",
    "hardcoded password": r"(?i)\b(?:password|passwd|secret)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
    "bearer literal": r"(?i)authorization\s*[:=]\s*[\"']Bearer\s+[A-Za-z0-9._\-]{20,}",
}

# Values that identify a specific person. Tune `IDENTITY_EXTRA` in a fork.
PII_PATTERNS: dict[str, str] = {
    "email address": r"\b[A-Za-z0-9._%+\-]+@(?!example\.(?:com|org)\b)"
                     r"[A-Za-z0-9.\-]+\.(?:com|org|net|edu|io|dev|co)\b",
    "US phone number": r"(?<![\d\-])(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}\b",
    # Context-bound on purpose. A bare ISO date also matches API version
    # headers (2023-06-01), changelog dates and test fixtures, and a check
    # that fires on those is one you learn to scroll past.
    "date of birth": r"(?i)(?:date[_\s-]?of[_\s-]?birth|\bdob\b|birth[_\s-]?date)"
                     r"[\"\'\s:=]{0,12}(?:19[5-9]\d|20[0-2]\d)-\d{2}-\d{2}",
    "street address": r"\b\d{1,5}\s+[A-Z][a-z]+\s+(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Boulevard|Blvd)\b",
    "US ZIP+state": r"\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b",
}

# Paths that must never be tracked. Checked against git, not against a filename.
MUST_BE_IGNORED = [
    "config/profile.yaml",
    "config/bank/master_resume.md",
    "config/bank/atoms.yaml",
    "config/bank/claims.yaml",
    "config/secrets.yaml",
    ".env",
    "data/applier.db",
    # SQLite runs in WAL mode, so the live rows also sit in these two. A
    # .gitignore of `data/*.db` matches neither, and `git add -A` will
    # happily stage a -wal file full of real application data. Found the
    # hard way; it stays on the list.
    "data/applier.db-wal",
    "data/applier.db-shm",
    "config/settings.local.yaml",
    ".browser-profile/",
    "applications/",
    "out/",
    "private/",
    "token.json",
    "credentials.json",
]

# Files allowed to mention a real-looking address: the audit's own patterns.
SELF = {"scripts/audit_privacy.py"}

# Canonically synthetic values. Deliberately a tiny, explicit list rather than
# an exemption for tests/ as a whole -- a blanket exemption is somewhere real
# data can hide.
SYNTHETIC = {
    "1900-01-01",           # test date of birth
    "2000-01-01",           # earlier test fixture, still reachable in git history
    "555-555-5555",         # reserved test phone range
    "+1 555-555-5555",
}


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True,
                          errors="replace", cwd=ROOT).stdout


def tracked_files() -> list[str]:
    return [f for f in sh("git", "ls-files").split("\n") if f.strip()]


class Audit:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []
        self.checks = 0

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    # ------------------------------------------------------------------ #
    def scan_tracked(self) -> None:
        print("1. secrets and personal data in tracked files")
        files = tracked_files()
        print(f"   scanning {len(files)} tracked file(s)")
        for f in files:
            if f in SELF:
                continue
            path = ROOT / f
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, IsADirectoryError):
                continue
            for label, pat in SECRET_PATTERNS.items():
                for m in re.finditer(pat, text):
                    self.checks += 1
                    self.fail(f"SECRET  {label} in {f}: {m.group(0)[:16]}...")
            for label, pat in PII_PATTERNS.items():
                for m in re.finditer(pat, text):
                    self.checks += 1
                    if any(syn in m.group(0) for syn in SYNTHETIC):
                        continue
                    self.fail(f"PII     {label} in {f}: {m.group(0)[:40]}")

    def scan_history(self) -> None:
        print("2. secrets and personal data in git history")
        hist = sh("git", "log", "--all", "-p", "--no-color")
        if not hist.strip():
            self.warn("no git history to scan")
            return
        print(f"   scanning {len(hist) // 1024} KB of history")
        # Commit metadata legitimately contains the author's own address.
        body = "\n".join(l for l in hist.split("\n")
                         if not l.startswith(("Author:", "Commit:", "    Co-Authored-By:")))
        for label, pat in SECRET_PATTERNS.items():
            for m in re.finditer(pat, body):
                self.fail(f"SECRET IN HISTORY  {label}: {m.group(0)[:16]}... "
                          f"(rewriting the tree does NOT remove this)")
        for label, pat in PII_PATTERNS.items():
            hits = {m.group(0) for m in re.finditer(pat, body)}
            hits = {h for h in hits if not any(syn in h for syn in SYNTHETIC)}
            for h in list(hits)[:5]:
                self.fail(f"PII IN HISTORY  {label}: {h[:40]}")

    def check_ignored(self) -> None:
        print("3. gitignore coverage for local-only paths")
        tracked = set(tracked_files())
        for p in MUST_BE_IGNORED:
            self.checks += 1
            ignored = subprocess.run(["git", "check-ignore", "-q", p],
                                     cwd=ROOT).returncode == 0
            is_tracked = p in tracked
            if is_tracked:
                self.fail(f"TRACKED but must be local-only: {p}")
            elif not ignored:
                self.warn(f"not matched by .gitignore (create it and it WILL be "
                          f"committed): {p}")

    def check_artifacts(self) -> None:
        print("4. generated artifacts")
        bad = [f for f in tracked_files()
               if (f.endswith((".pdf", ".db", ".sqlite3", ".log"))
                   or ".db-" in f or ".sqlite3-" in f)
               and not f.startswith("docs/")]
        for f in bad:
            self.checks += 1
            self.fail(f"GENERATED ARTIFACT tracked: {f}")

    def check_templates(self) -> None:
        print("5. template hygiene")
        for f in tracked_files():
            if ".example." not in f:
                continue
            self.checks += 1
            text = (ROOT / f).read_text(encoding="utf-8", errors="replace")
            for label, pat in PII_PATTERNS.items():
                for m in re.finditer(pat, text):
                    self.fail(f"TEMPLATE {f} contains real-looking {label}: "
                              f"{m.group(0)[:40]}")

    # ------------------------------------------------------------------ #
    def run(self) -> int:
        print("=" * 70)
        print("privacy audit")
        print("=" * 70)
        for step in (self.scan_tracked, self.scan_history, self.check_ignored,
                     self.check_artifacts, self.check_templates):
            step()
        print()
        print("=" * 70)
        if self.failures:
            print(f"FAILED — {len(self.failures)} issue(s)")
            for f in self.failures:
                print(f"  ! {f}")
        else:
            print("PASS — no secrets or personal data in tracked files or history")
        if self.warnings:
            print(f"\n{len(self.warnings)} warning(s):")
            for w in self.warnings:
                print(f"  - {w}")
        print("=" * 70)
        return 1 if self.failures else 0


if __name__ == "__main__":
    sys.exit(Audit().run())
