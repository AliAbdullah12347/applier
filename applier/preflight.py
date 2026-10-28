"""Everything that must be true before letting this run unattended.

`doctor` answers "is this installed". That is a different question from "is it
safe to leave running for six hours", and the gap between them is where an
overnight run is wasted. A missing TeX distribution does not announce itself
until the first application; an expired API key looks exactly like a working
one until something calls it; a content bank that fails to import leaves the
tailor silently falling back to a generic resume for every posting.

So this does the expensive things on purpose: it renders a real PDF, makes a
real model call, and opens a real browser. Minutes here, against hours and a
day's application quota there.

Checks are either **blocking** -- the run would do damage or waste the quota --
or advisory. `applier run` refuses to start on a blocking failure, and says
which one.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Config, data_path, project_paths


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = False
    fix: str = ""

    @property
    def state(self) -> str:
        return "ok" if self.ok else ("BLOCKED" if self.blocking else "warn")


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.blocking]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking_failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "blocking": [c.name for c in self.blocking_failures],
            "checks": [
                {"name": c.name, "ok": c.ok, "state": c.state,
                 "detail": c.detail, "blocking": c.blocking, "fix": c.fix}
                for c in self.checks
            ],
        }


def _safe(fn: Callable[[], Check], name: str, blocking: bool) -> Check:
    """A check that throws is a failed check, never a crashed preflight."""
    try:
        return fn()
    except Exception as exc:                            # noqa: BLE001
        return Check(name, False, f"{type(exc).__name__}: {exc}"[:200],
                     blocking=blocking,
                     fix="This check could not complete; the underlying problem "
                         "is usually the real one.")


# --------------------------------------------------------------------------- #
# individual checks
# --------------------------------------------------------------------------- #
def _check_profile(profile: Config) -> Check:
    from .config import unanswered

    gaps = unanswered(profile)
    critical = [g for g in gaps if g.startswith(
        ("identity.full_name", "identity.email", "identity.phone",
         "work_authorization."))]
    if critical:
        return Check("profile", False,
                     f"{len(critical)} answer(s) still unset: {', '.join(critical[:4])}",
                     blocking=True,
                     fix="Settings > Profile, or `applier setup`. These go onto "
                         "forms verbatim, so a placeholder is worse than a gap.")
    if gaps:
        return Check("profile", True,
                     f"complete where it matters ({len(gaps)} optional field(s) unset)")
    return Check("profile", True, "complete")


def _check_bank(settings: Config) -> Check:
    from .tailor.bank import Bank

    bank_dir = data_path(settings.get("tailor.bank_path", "config/bank"))
    if not (bank_dir / "atoms.yaml").exists():
        return Check("content bank", False, f"no atoms.yaml in {bank_dir}",
                     blocking=True,
                     fix="Resume tab > Save and rebuild, or `applier bank import`.")
    bank = Bank(bank_dir)
    blocking = bank.blocking_problems()
    if blocking:
        return Check("content bank", False,
                     f"{len(blocking)} lint problem(s): {blocking[0]}"[:200],
                     blocking=True,
                     fix="`applier bank lint` lists them all.")

    # A fresh install is seeded with the shipped example so that nothing
    # crashes before a profile exists. Running on it produces a resume with
    # four placeholder bullets -- which then fails verification for having
    # almost no extractable text, and reports that as a rendering problem.
    # The rendering is fine. The bank is a sample.
    from .config import ASSETS

    example = ASSETS / "config" / "bank" / "atoms.example.yaml"
    mine = bank_dir / "atoms.yaml"
    try:
        is_example = (example.is_file() and mine.is_file()
                      and example.read_bytes() == mine.read_bytes())
    except OSError:
        is_example = False
    if is_example:
        return Check(
            "content bank", False,
            f"still the bundled example ({len(bank.atoms)} placeholder bullets)",
            blocking=True,
            fix="Every application would carry a sample resume. Paste your "
                "master resume into the Resume tab and press Save and rebuild, "
                "or bring an existing install across with `applier backup` "
                "and `applier backup --restore <file>`.")

    return Check("content bank", True,
                 f"{len(bank.atoms)} bullets, {len(getattr(bank, 'roles', {}))} entries, lint clean")


def _check_renderer(settings: Config, profile: Config) -> Check:
    """Render a real resume. The only check that proves the tailor works."""
    if not shutil.which("pdflatex"):
        return Check("resume rendering", False, "pdflatex is not on PATH",
                     blocking=True,
                     fix="Install TeX Live or MiKTeX. Without it every "
                         "application falls back to an untailored resume.")
    from .tailor.pipeline import build_resume
    from .tailor.render import squeezed_words

    job = {"id": 0, "company": "Preflight", "title": "Software Engineering Intern",
           "description": "Python, TypeScript, REST APIs, automated testing."}
    with tempfile.TemporaryDirectory() as tmp:
        pdf, report, _sel = build_resume(job, settings, profile, Path(tmp))
        if not report.ok:
            return Check("resume rendering", False,
                         f"verification failed: {'; '.join(report.failures)[:160]}",
                         blocking=True,
                         fix="`python scripts/check_families.py` renders one per "
                             "family and shows which.")
        squeezed = squeezed_words(pdf)
        if squeezed:
            return Check("resume rendering", False,
                         f"words run together in the text layer: {squeezed[0][:60]}",
                         blocking=True,
                         fix="The line is too tight for a parser to split. "
                             "Shorten the bank entry it comes from.")
        return Check("resume rendering", True,
                     f"one page, verified, {report.chars} extractable characters")


def _check_model(settings: Config, profile: Config) -> Check:
    """Make a real call. A rejected key looks identical to a good one until used."""
    from .llm import Router

    router = Router(settings, profile, None)
    reply = router.complete(
        "Reply with exactly the word: ready.",
        "Say ready.", task=None)
    text = (reply.text or "").strip().lower()
    if "ready" not in text:
        return Check("language model", False,
                     f"responded, but not usefully: {text[:80]!r}", blocking=False,
                     fix="The chain answered, so the key works. Check the model "
                         "name in Settings > Model chain.")
    return Check("language model", True,
                 f"{reply.provider}/{reply.model} answered "
                 f"({reply.output_tokens} output tokens)")


def _check_browser(settings: Config) -> Check:
    """Only blocking when the autonomy level will actually open a browser."""
    from . import autonomy

    level = autonomy.current(settings)
    needs = level != autonomy.INVOLVED
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Check("browser", False, "playwright is not installed",
                     blocking=needs, fix="pip install playwright")
    try:
        with sync_playwright() as pw:
            from .browsers import launch_kwargs
            browser = pw.chromium.launch(headless=True, **launch_kwargs())
            browser.close()
    except Exception as exc:                            # noqa: BLE001
        # The old message quoted the path Playwright's driver *computed* and
        # said nothing about the path this program actually searched. When
        # those two disagree -- which is the entire reason applier.browsers
        # exists -- the report described a file nobody had looked for, and
        # the only remedy on offer was a terminal command from a library the
        # reader never installed. Two unhelpful things at once.
        from .browsers import describe_failure, diagnose

        detail = str(exc).splitlines()[0]
        d = diagnose()
        return Check(
            "browser", False,
            f"chromium will not start: {describe_failure(detail)}"[:400],
            blocking=needs,
            fix=("Press \u201cInstall browser\u201d below, or run "
                 "`applier browsers install`. It downloads Chromium once, "
                 "into the per-user folder this program searches "
                 f"({d['root']}), and every future version finds it. "
                 "`applier browsers where` prints what is there now."))
    return Check("browser", True,
                 "chromium starts" + ("" if needs else " (not needed at this autonomy level)"))


def _check_disk() -> Check:
    paths = project_paths()
    free = shutil.disk_usage(paths["root"]).free / 1_073_741_824
    if free < 0.5:
        return Check("disk space", False, f"{free:.1f} GB free", blocking=True,
                     fix="Each application writes a PDF and screenshots. Under "
                         "half a gigabyte, renders start failing silently.")
    return Check("disk space", free >= 2.0, f"{free:.1f} GB free",
                 blocking=False,
                 fix="" if free >= 2.0 else "Comfortable is a couple of gigabytes.")


def _check_database() -> Check:
    from .db import get_db

    db = get_db()
    db.run("INSERT INTO meta(key,value) VALUES('preflight',?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("ok",))
    mode = db.one("PRAGMA journal_mode")
    jobs = db.one("SELECT COUNT(*) c FROM jobs")["c"]
    return Check("database", True,
                 f"writable, {jobs} postings, journal={tuple(mode)[0] if mode else '?'}")


def _check_autonomy(settings: Config) -> Check:
    """Not a pass/fail -- a statement of what is about to happen."""
    from . import autonomy

    level = autonomy.LEVELS[autonomy.current(settings)]
    cap = settings.get("apply.max_per_day", 25)
    per_hour = settings.get("apply.max_per_hour", 6)
    detail = (f"{level.label}: {'submits automatically' if level.submits else 'never submits'}"
              f", up to {cap}/day and {per_hour}/hour")
    return Check("autonomy", True, detail,
                 fix="" if not level.submits else
                     "This level sends applications without asking. "
                     "Settings > Autonomy to change it.")


def _check_caps(settings: Config) -> Check:
    from .score.gates import cap_key_for

    caps = settings.get("gates.application_caps", {}) or {}
    if not caps:
        return Check("employer caps", True, "none configured")
    probe = cap_key_for({"company": next(iter(caps)).split(".")[0].title(),
                         "company_domain": "job-boards.greenhouse.io"}, caps)
    if not probe:
        return Check("employer caps", False,
                     "configured, but not matching ATS-hosted postings",
                     blocking=False,
                     fix="Caps resolve by employer name as well as domain; this "
                         "suggests that resolution has regressed.")
    return Check("employer caps", True, f"{len(caps)} configured and resolving")


# --------------------------------------------------------------------------- #
def run_preflight(settings: Config, profile: Config, *, deep: bool = True) -> Report:
    """Every check. `deep=False` skips the ones that cost time or quota."""
    report = Report()
    report.checks.append(_safe(lambda: _check_profile(profile), "profile", True))
    report.checks.append(_safe(lambda: _check_bank(settings), "content bank", True))
    report.checks.append(_safe(_check_database, "database", True))
    report.checks.append(_safe(_check_disk, "disk space", True))
    report.checks.append(_safe(lambda: _check_autonomy(settings), "autonomy", False))
    report.checks.append(_safe(lambda: _check_caps(settings), "employer caps", False))
    if deep:
        # Rendering from the example bank fails for having almost no text,
        # which is true and useless: it reports the same problem the bank
        # check just reported, in terms that sound like a broken renderer.
        # One cause, one message.
        bank_is_example = any(
            c.name == "content bank" and not c.ok and "example" in c.detail
            for c in report.checks)
        if bank_is_example:
            report.checks.append(Check(
                "resume rendering", True,
                "not checked — there is nothing real in the bank to render yet"))
        else:
            report.checks.append(
                _safe(lambda: _check_renderer(settings, profile),
                      "resume rendering", True))
        report.checks.append(
            _safe(lambda: _check_model(settings, profile), "language model", True))
        report.checks.append(_safe(lambda: _check_browser(settings), "browser", False))
    return report
