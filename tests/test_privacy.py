"""The privacy audit, as a hard test gate.

This repository is published. A secret or a personal detail that reaches the
remote is not undone by a later commit — `git rm` leaves the old blob reachable
and a published key is compromised the moment it lands.

So the audit runs as a test rather than as a ritual somebody remembers to
perform. If this fails, do not push.

Note on the detector test below: the sample secrets are ASSEMBLED AT RUNTIME
from fragments rather than written as literals. An earlier version of this file
pasted realistic-looking keys straight into the source, which is precisely the
thing the audit exists to prevent — and it tripped the audit on itself. A test
for a secret scanner must not contain a scannable secret.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / "scripts" / "audit_privacy.py"


def _audit():
    spec = importlib.util.spec_from_file_location("audit_privacy", AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_audit_script_exists():
    assert AUDIT.exists(), "the privacy audit script is missing"


def test_no_secrets_or_pii_anywhere():
    """Tracked files AND git history. History is the half people forget."""
    result = subprocess.run([sys.executable, str(AUDIT)],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, (
        "PRIVACY AUDIT FAILED — do not push.\n" + result.stdout + result.stderr)


def test_personal_files_are_not_tracked():
    """The specific files that hold a real person's life."""
    tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                                 text=True, cwd=ROOT).stdout.split())
    for p in ("config/profile.yaml", "config/bank/master_resume.md",
              "config/bank/atoms.yaml", "config/bank/claims.yaml"):
        assert p not in tracked, f"{p} is tracked and contains personal data"


def test_templates_are_shipped_for_every_private_file():
    """A private file with no template makes the project unusable to anyone else."""
    tracked = set(subprocess.run(["git", "ls-files"], capture_output=True,
                                 text=True, cwd=ROOT).stdout.split())
    for template in ("config/profile.example.yaml",
                     "config/bank/master_resume.example.md",
                     "config/bank/atoms.example.yaml",
                     "config/bank/claims.example.yaml"):
        assert template in tracked, f"missing template: {template}"


def test_secret_patterns_catch_realistic_shapes():
    """Guard the guard: a weakened pattern must fail here, not pass silently.

    Samples are built from fragments so this file never contains a literal that
    a scanner — ours or GitHub's — would flag.
    """
    mod = _audit()
    alnum = "abcdefghijklmnopqrstuvwxyz0123456789"
    samples = [
        ("Google/Gemini API key", "AIza" + "Sy" + alnum[:33]),
        ("Google OAuth token", "AQ." + "Ab" + alnum[:38]),
        ("OpenAI key", "sk-" + alnum[:32]),
        ("GitHub token", "ghp_" + alnum[:36]),
        ("AWS access key", "AKIA" + "ABCDEFGHIJKLMNOP"),
        ("private key block", "-" * 5 + "BEGIN RSA PRIVATE " + "KEY"),
    ]
    for label, sample in samples:
        assert re.search(mod.SECRET_PATTERNS[label], sample), (
            f"the {label} pattern no longer detects a realistic secret")


def test_audit_ignores_benign_lookalikes():
    """And must not fire on things that merely resemble secrets."""
    mod = _audit()
    benign = [
        ('anthropic-version": "2023-06-01"', "date of birth"),
        ("# last reviewed 2026-09-21", "date of birth"),
    ]
    for text, label in benign:
        assert not re.search(mod.PII_PATTERNS[label], text), (
            f"{label!r} fires on benign text: {text!r}")
