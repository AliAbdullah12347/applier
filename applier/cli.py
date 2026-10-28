"""applier — command line interface.

Everything runs through one entry point:

    applier gui              open the app; everything below, with a screen
    applier setup            one-time wizard; asks for anything missing
    applier doctor           check keys, deps, disk, config -- run this first
    applier discover         pull new postings from all enabled sources
    applier rank             score what's been discovered
    applier show             what's queued, with scores and reasons
    applier apply <url>      one-shot: any job link, start to finish
    applier run              autonomous loop: discover -> rank -> apply
    applier asks             clear the ask queue (asked once, never again)
    applier status           funnel stats and recent outcomes
    applier config set-key   store an API key in the OS keychain

    applier outreach harvest --github-org <org> | --repo o/r | --team-page <url> | --csv <f>
    applier outreach draft   write messages for harvested contacts (never sends)
    applier outreach list    show contacts and their stage

    applier bank import      rebuild the content bank from master_resume.md
    applier bank lint        check every phrasing is renderable and traceable
    applier bank preview     what a given job description would select
"""

from __future__ import annotations

import getpass
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import (data_path, 
    CONFIG_DIR, Config, ConfigError, get_secret, load_profile, load_settings,
    project_paths, save_profile, set_secret, unanswered,
)

app = typer.Typer(add_completion=False, help="Autonomous end-to-end job applications.")
config_app = typer.Typer(help="Configuration and secrets.")
app.add_typer(config_app, name="config")
reach_app = typer.Typer(help="Referrals and networking. Drafts only -- never sends.")
app.add_typer(reach_app, name="outreach")
bank_app = typer.Typer(help="The master resume and content bank.")
app.add_typer(bank_app, name="bank")
con = Console()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _load() -> tuple[Config, Config]:
    try:
        return load_settings(), load_profile()
    except ConfigError as e:
        con.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(1)


PROMPTS: dict[str, tuple[str, str]] = {
    # dotted path -> (question, hint)
    "identity.email_personal": (
        "Personal email (NOT your .edu)",
        "Used to register accounts on employer sites and to watch for verification "
        "links. Prefer a personal address: university Workspace admins often block "
        "third-party apps, which would silently break this.",
    ),
    "identity.date_of_birth": ("Date of birth (YYYY-MM-DD)", "Some applications require it. Stored locally only."),
    "identity.pronouns": ("Pronouns (or press Enter to skip)", "Only used when a form asks."),
    "address.line1": ("Street address", "Your current term-time address."),
    "address.permanent.line1": ("Permanent/home address (street)", "Some international forms need both."),
    "address.permanent.postal_code": ("Permanent postal code", ""),
    "education[0].cip_code": (
        "Programme CIP code (international students only)",
        "From your I-20 if you have one. Affects post-study work eligibility in "
        "some jurisdictions. Leave blank if not applicable.",
    ),
    "compensation.expected_hourly_usd": ("Expected hourly rate USD (e.g. 45)", "Most forms require a number."),
    "compensation.expected_salary_usd": ("Expected annual salary USD (for full-time later)", ""),
    "demographics.gender": ("Gender for EEO forms (or Enter to decline)", "Voluntary. Blank = decline to self-identify."),
    "demographics.ethnicity": ("Ethnicity for EEO forms (or Enter to decline)", "Voluntary."),
    "demographics.disability_status": ("Disability status (or Enter to decline)", "Voluntary."),
}


def _get_dotted(cfg: Config, dotted: str):
    if "[" in dotted:  # education[0].cip_code
        base, _, rest = dotted.partition("[")
        idx, _, tail = rest.partition("]")
        lst = cfg.get(base, [])
        try:
            node = lst[int(idx)]
        except (IndexError, ValueError):
            return None
        for part in tail.lstrip(".").split("."):
            node = node.get(part) if isinstance(node, dict) else None
        return node
    return cfg.get(dotted, None)


def _set_dotted(cfg: Config, dotted: str, value) -> None:
    if "[" in dotted:
        base, _, rest = dotted.partition("[")
        idx, _, tail = rest.partition("]")
        lst = cfg.get(base, [])
        node = lst[int(idx)]
        parts = tail.lstrip(".").split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        cfg.set(base, lst)
    else:
        cfg.set(dotted, value)


# --------------------------------------------------------------------------- #
@app.command()
def setup() -> None:
    """One-time wizard. Asks for anything missing, then never asks again."""
    con.print(Panel.fit("[bold]applier setup[/bold]\nAnswer once. Everything is stored locally.",
                        border_style="cyan"))

    profile_path = CONFIG_DIR / "profile.yaml"
    if not profile_path.exists():
        example = CONFIG_DIR / "profile.example.yaml"
        shutil.copy(example, profile_path)
        con.print(f"[green]Created[/green] {profile_path} from the template.")

    settings = load_settings()
    profile = load_profile(reload=True)

    # ---- API key -------------------------------------------------------- #
    key_env = settings.get("llm.primary.api_key_env", "GEMINI_API_KEY")
    provider = settings.get("llm.primary.provider", "gemini")
    if not get_secret(key_env):
        con.print(f"\n[bold]LLM key[/bold] — provider is [cyan]{provider}[/cyan]")
        if provider == "gemini":
            con.print("  Get a free key at [link]https://aistudio.google.com/apikey[/link]")
        val = getpass.getpass(f"  {key_env} (hidden, Enter to skip): ").strip()
        if val:
            set_secret(key_env, val)
            con.print("  [green]stored in the OS keychain[/green]")
    else:
        con.print(f"[green]OK[/green] {key_env} already stored.")

    # ---- email app password --------------------------------------------- #
    if not get_secret("APPLIER_IMAP_PASSWORD"):
        con.print("\n[bold]Email verification[/bold] (optional but enables hands-free signup)")
        con.print("  An app password for your personal inbox, so the system can click the")
        con.print("  confirmation links employers email you. Gmail: myaccount.google.com/apppasswords")
        val = getpass.getpass("  APPLIER_IMAP_PASSWORD (hidden, Enter to skip): ").strip()
        if val:
            set_secret("APPLIER_IMAP_PASSWORD", val)
            con.print("  [green]stored[/green]")

    # ---- profile gaps ---------------------------------------------------- #
    missing = unanswered(profile)
    if missing:
        con.print(f"\n[bold]{len(missing)} profile field(s) to fill.[/bold] Press Enter to skip any.\n")
    changed = False
    for dotted in missing:
        q, hint = PROMPTS.get(dotted, (dotted.replace(".", " → "), ""))
        if hint:
            con.print(f"[dim]{hint}[/dim]")
        ans = typer.prompt(f"  {q}", default="", show_default=False).strip()
        if ans:
            _set_dotted(profile, dotted, ans)
            changed = True
        con.print("")

    if changed:
        save_profile(profile)
        con.print(f"[green]Saved[/green] {profile_path}")

    still = unanswered(load_profile(reload=True))
    if still:
        con.print(f"[yellow]{len(still)} field(s) still unset[/yellow] — "
                  f"the system will ask again only if a form actually needs them.")
    con.print("\n[bold green]Setup complete.[/bold green] Next: [cyan]applier doctor[/cyan]")


# --------------------------------------------------------------------------- #
@app.command()
def gui(
    port: int = typer.Option(8765, help="Port on localhost. Takes the next free one if busy."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    verbose: bool = typer.Option(False, "--verbose", help="Log every HTTP request."),
) -> None:
    """Open the app. Everything the CLI does, with a screen in front of it.

    Serves on 127.0.0.1 only, behind a token minted fresh each launch. It is
    not reachable from another machine and there is no flag to make it so:
    the pages here show a date of birth, a home address and an immigration
    status, and the only authentication is that token.
    """
    from .web import serve
    try:
        serve(port=port, open_browser=not no_browser, verbose=verbose)
    except OSError as e:
        con.print(f"[red]Could not start the server:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def desktop(
    port: int = typer.Option(8765, help="Port on localhost. Takes the next free one if busy."),
    verbose: bool = typer.Option(False, "--verbose", help="Log every HTTP request."),
) -> None:
    """Open applier in its own window, rather than in a browser tab.

    Same server, same endpoints, same token as `applier gui` -- this is the
    browser tab with a window frame around it and no address bar. If no window
    toolkit is available it falls back to your browser and says so.
    """
    from .desktop import run
    raise typer.Exit(run(port=port, verbose=verbose))


@app.command(hidden=True)
def selftest(
    code: str = typer.Option(..., "--code", help="Python to execute inside this build."),
) -> None:
    """Run a snippet inside this interpreter. Used to verify a packaged build.

    A frozen application is a different interpreter with a different module
    graph, and the parts most likely to be missing are the ones reached by
    name rather than by import -- the PDF verifier, the provider router, the
    ATS adapters. There is no way to check those from outside the bundle, so
    the bundle is asked directly.
    """
    exec(compile(code, "<selftest>", "exec"), {"__name__": "__selftest__"})


@app.command()
def extension() -> None:
    """Where the Chrome extension lives, and how to pair it.

    The extension talks to this machine and nowhere else. It has no remote
    endpoint, loads no code from a network, and can do nothing at all unless
    `applier gui` is running.
    """
    from .config import ROOT
    ext = ROOT / "extension"
    if not (ext / "manifest.json").exists():
        con.print(f"[red]No extension found at[/red] {ext}")
        raise typer.Exit(1)

    con.print(Panel.fit(str(ext), title="load this folder", border_style="cyan"))
    con.print("""
[bold]Install[/bold] (once)
  1. Open [cyan]chrome://extensions[/cyan]
  2. Turn on [bold]Developer mode[/bold], top right
  3. Click [bold]Load unpacked[/bold] and choose the folder above

[bold]Pair[/bold] (again after every server restart)
  4. Run [cyan]python -m applier gui[/cyan] and copy the link it prints
  5. Click the extension, then [bold]Pair with applier[/bold], and paste it

The token is minted fresh each launch, so a leaked one stops working the
moment you restart the server. That is the trade for not storing a secret
on disk.

[bold]Use[/bold]
  Open any job posting, click the extension, pick how much it should do,
  press Apply. It runs on your machine; switch tabs and carry on. You get a
  notification when it finishes.
""")


@app.command()
def doctor() -> None:
    """Check everything before a real run."""
    from .db import get_db
    from .llm import Router

    ok = True
    t = Table(title="applier doctor", show_lines=False)
    t.add_column("Check"); t.add_column("Result"); t.add_column("Detail", overflow="fold")

    def row(name, good, detail=""):
        nonlocal ok
        ok = ok and good
        t.add_row(name, "[green]OK[/green]" if good else "[red]FAIL[/red]", detail)

    # config
    try:
        settings, profile = load_settings(), load_profile()
        row("config", True, f"{settings.source}")
    except ConfigError as e:
        row("config", False, str(e)[:120]); con.print(t); raise typer.Exit(1)

    # disk -- research flagged this as a hard blocker on this machine
    free_gb = shutil.disk_usage(Path.home().anchor).free / 1e9
    row("disk space", free_gb > 5, f"{free_gb:.1f} GB free"
        + ("" if free_gb > 5 else "  — Playwright + Chromium needs ~1 GB"))

    # deps
    for mod, label in [("httpx", "httpx"), ("yaml", "PyYAML"), ("rapidfuzz", "rapidfuzz"),
                       ("jinja2", "Jinja2"), ("playwright", "playwright"), ("keyring", "keyring")]:
        try:
            __import__(mod); row(f"dep: {label}", True)
        except ImportError:
            row(f"dep: {label}", False, "pip install -r requirements.txt")

    # pdflatex
    latex = shutil.which("pdflatex")
    row("pdflatex", bool(latex), latex or "needed to render tailored resumes")

    # llm
    try:
        db = get_db()
        r = Router(settings, profile, db)
        health = r.health()
        ready = [h for h in health if h["ready"]]
        row("LLM provider", bool(ready),
            ", ".join(f"{h['provider']}/{h['model']}" + ("" if h["ready"] else " (no key)")
                      for h in health))
    except Exception as e:
        row("LLM provider", False, str(e)[:120])

    # profile completeness
    miss = unanswered(profile)
    row("profile", not miss, "complete" if not miss else f"{len(miss)} unset: " + ", ".join(miss[:4]))

    # legal block -- the one that matters most
    wa = profile.get("work_authorization", {})
    cpt = wa.get("work_permit_status", wa.get("cpt_status", "UNRESOLVED"))
    row("work authorization", bool(wa.get("citizenship")),
        f"status set, work permit: {cpt}"
        + ("  — confirm with your institution in writing" if cpt == "UNRESOLVED" else ""))

    con.print(t)
    con.print("\n[bold green]Ready.[/bold green]" if ok else
              "\n[yellow]Fix the failures above, then re-run.[/yellow]")


# --------------------------------------------------------------------------- #
@app.command()
def apply(
    url: str = typer.Argument(..., help="Any job listing URL."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Prepare the documents; never open a browser."),
    review: bool = typer.Option(False, "--review", help="Fill every field, then hand you the browser."),
    submit: bool = typer.Option(False, "--submit", help="Actually send it. This cannot be undone."),
) -> None:
    """One-shot: give it a link, it does the rest.

    Defaults to whatever the autonomy level says, which ships as "involved" --
    documents prepared, nothing opened. The three flags override that for this
    one run, which is deliberately easier to reason about than editing
    settings and forgetting.
    """
    settings, profile = _load()
    if sum(map(bool, (dry_run, review, submit))) > 1:
        con.print("[red]Pick one of --dry-run, --review, --submit.[/red]")
        raise typer.Exit(1)
    if dry_run:
        settings.set("apply.mode", "dry_run")
    elif review:
        settings.set("apply.mode", "review")
    elif submit:
        # Said out loud, because the whole system is otherwise built to make
        # this hard to do by accident.
        settings.set("apply.mode", "auto")
        con.print("[yellow]--submit: this will send the application. "
                  "There is no undo.[/yellow]")

    from .pipeline import apply_to_url
    try:
        result = apply_to_url(url, settings, profile, console=con)
    except Exception as e:
        con.print(f"[red]Failed:[/red] {e}")
        raise typer.Exit(1)
    con.print(Panel.fit(str(result), border_style="green"))


@app.command()
def discover(limit: int = typer.Option(0, help="Stop after N new postings (0 = no limit).")) -> None:
    """Pull new postings from every enabled source."""
    settings, profile = _load()
    from .pipeline import discover_jobs
    n = discover_jobs(settings, profile, limit=limit or None, console=con)
    con.print(f"[green]{n}[/green] new postings stored.")


@app.command()
def rank(top: int = typer.Option(25, help="How many to display.")) -> None:
    """Score discovered jobs against your profile and gates."""
    settings, profile = _load()
    from .pipeline import rank_jobs
    rows = rank_jobs(settings, profile, console=con)
    t = Table(title=f"Top {min(top, len(rows))} matches")
    t.add_column("Score", justify="right"); t.add_column("Company")
    t.add_column("Title", overflow="fold"); t.add_column("Loc"); t.add_column("Why", overflow="fold")
    for r in rows[:top]:
        t.add_row(f"{r['score']:.2f}", r["company"], r["title"],
                  r.get("location") or "", r.get("why", ""))
    con.print(t)


@app.command()
def backup(
    to: Path = typer.Option(None, "--to", help="Where to write it. Defaults to out/."),
    restore_from: Path = typer.Option(None, "--restore", help="Unpack an archive instead."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace existing files on restore."),
) -> None:
    """Copy out the three things here that cannot be rebuilt.

    Your profile, your content bank and the record of what you have applied
    to. The code is in git and the job table can be rediscovered; those three
    are months of work and a single directory. API keys are not included --
    they live in the OS keychain, and an archive containing them would be a
    plaintext secret in a Downloads folder.
    """
    _load()
    from .backup import create, inspect as inspect_archive, restore as do_restore

    if restore_from:
        if not restore_from.exists():
            con.print(f"[red]no such archive: {restore_from}[/red]")
            raise typer.Exit(1)
        meta = inspect_archive(restore_from)
        con.print(f"[dim]{meta.get('entries', '?')} entries, written "
                  f"{meta.get('created', 'unknown')}[/dim]")
        try:
            written = do_restore(restore_from, overwrite=overwrite)
        except FileExistsError as e:
            con.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(1)
        except ValueError as e:
            con.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        con.print(f"[green]restored[/green] {len(written)} file(s)")
        return

    result = create(to)
    con.print(f"[green]backed up[/green] {result.files} file(s), "
              f"{result.megabytes:.1f} MB")
    con.print(f"  {result.path}")
    con.print("[dim]API keys are not in it. After a restore on a new machine, "
              "set them again with `applier config set-key`.[/dim]")


@app.command()
def mode(
    to: str = typer.Argument(None, help="internship or job. Omit to see where you are."),
) -> None:
    """Internships, or jobs. Two searches that want different resumes.

    An internship is gated on still being enrolled; a new-grad or full-time
    posting is gated on having finished. Postings of the other kind are set
    aside rather than discarded, so switching back costs nothing.
    """
    _load()
    from .web import api as _api

    try:
        data = _api.set_mode({"mode": to}) if to else _api.get_mode()
    except Exception as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if to:
        con.print(f"[green]switched[/green] to {data['mode']}\n")

    t = Table(box=None, pad_edge=False)
    for c in ("", "mode", "covers", "postings"):
        t.add_column(c)
    for m in data["modes"]:
        here = "[cyan]>[/cyan]" if m["key"] == data["mode"] else " "
        t.add_row(here, m["label"], ", ".join(m["kinds"]), str(m["postings"]))
    con.print(t)
    con.print(f"\n[dim]{next(m['blurb'] for m in data['modes'] if m['key'] == data['mode'])}[/dim]")
    if to:
        con.print("[dim]Run `applier rank` to refill the queue.[/dim]")


@app.command()
def preflight(
    quick: bool = typer.Option(False, "--quick", help="Skip the render, the model call and the browser."),
) -> None:
    """Everything that must hold before leaving this running unattended.

    `doctor` answers "is it installed". This answers "will six hours of this
    waste a day's application quota", which is a different question: it
    renders a real PDF, makes a real model call, and starts a real browser.
    Minutes here against hours there.
    """
    settings, profile = _load()
    from .preflight import run_preflight

    report = run_preflight(settings, profile, deep=not quick)
    t = Table(title="preflight", box=None, pad_edge=False)
    for c in ("", "check", "detail"):
        t.add_column(c)
    marks = {"ok": "[green]ok[/green]", "warn": "[yellow]warn[/yellow]",
             "BLOCKED": "[red]BLOCK[/red]"}
    for c in report.checks:
        t.add_row(marks[c.state], c.name, c.detail[:74])
    con.print(t)

    for c in report.checks:
        if not c.ok and c.fix:
            con.print(f"\n[dim]{c.name}:[/dim] {c.fix}")

    if report.ok:
        con.print("\n[bold green]Ready to run unattended.[/bold green]")
    else:
        con.print(f"\n[bold red]Not ready[/bold red] — "
                  f"{len(report.blocking_failures)} blocking problem(s).")
        raise typer.Exit(1)


@app.command()
def run(
    max_apps: int = typer.Option(0, help="Stop after N applications (0 = use settings cap)."),
    once: bool = typer.Option(False, "--once", help="One cycle then exit."),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight", help="Start even if preflight finds a blocking problem."),
) -> None:
    """Autonomous loop: discover, rank, tailor, apply. Leave it running."""
    settings, profile = _load()

    # The whole point of an unattended run is that nobody is watching it, so
    # the things that would make it useless are checked while someone still
    # is. Skippable, because a deliberate degraded run is a legitimate
    # choice -- but it has to be deliberate.
    if not skip_preflight:
        from .preflight import run_preflight
        report = run_preflight(settings, profile, deep=True)
        if not report.ok:
            con.print("[red]Preflight failed — not starting.[/red]")
            for c in report.blocking_failures:
                con.print(f"  [red]x[/red] {c.name}: {c.detail}")
                if c.fix:
                    con.print(f"      {c.fix}")
            con.print("\n[dim]`applier preflight` for the full report, or "
                      "--skip-preflight to run anyway.[/dim]")
            raise typer.Exit(1)
        con.print("[dim]preflight clear[/dim]")

    from .pipeline import autonomous_loop
    autonomous_loop(settings, profile, max_apps=max_apps or None, once=once, console=con)


@app.command()
def asks() -> None:
    """Answer anything the system could not resolve. Asked once, never again."""
    settings, profile = _load()
    from .apply.answers import AnswerBank
    from .db import get_db

    db = get_db()
    bank = AnswerBank(db, profile, settings)
    pending = bank.pending_asks()
    if not pending:
        con.print("[green]Nothing pending.[/green]")
        return

    con.print(f"[bold]{len(pending)} unanswered question(s).[/bold] "
              f"Each is stored permanently.\n")
    for item in pending:
        con.print(f"[dim]{item.get('context') or ''}[/dim]")
        opts = item.get("options_json")
        if opts:
            import json
            con.print(f"[dim]options: {', '.join(json.loads(opts))}[/dim]")
        ans = typer.prompt(f"  {item['question_text']}", default="", show_default=False).strip()
        if ans:
            bank.remember(item["question_text"], ans, source="user",
                          field_type=item.get("field_type") or "text")
            db.run("UPDATE ask_queue SET resolved_at=datetime('now') WHERE id=?", (item["id"],))
            con.print("  [green]remembered[/green]\n")
        else:
            con.print("  [dim]skipped[/dim]\n")


@app.command()
def status() -> None:
    """Funnel stats and recent activity."""
    _load()
    from .db import get_db
    db = get_db()

    t = Table(title="pipeline")
    t.add_column("Stage"); t.add_column("Count", justify="right")
    for label, sql in [
        ("discovered", "SELECT COUNT(*) c FROM jobs"),
        ("passed gates", "SELECT COUNT(*) c FROM jobs WHERE gate_status='pass'"),
        ("gate-rejected", "SELECT COUNT(*) c FROM jobs WHERE gate_status='rejected'"),
        ("queued", "SELECT COUNT(*) c FROM jobs WHERE status='queued'"),
        ("applied", "SELECT COUNT(*) c FROM applications WHERE status='submitted'"),
        ("needs input", "SELECT COUNT(*) c FROM applications WHERE status='needs_input'"),
        ("open asks", "SELECT COUNT(*) c FROM ask_queue WHERE resolved_at IS NULL"),
    ]:
        row = db.one(sql)
        t.add_row(label, str(row["c"] if row else 0))
    con.print(t)

    outcomes = db.q("SELECT outcome, COUNT(*) c FROM applications "
                    "WHERE outcome IS NOT NULL GROUP BY outcome")
    if outcomes:
        o = Table(title="outcomes")
        o.add_column("Outcome"); o.add_column("Count", justify="right")
        for r in outcomes:
            o.add_row(r["outcome"], str(r["c"]))
        con.print(o)

    # The diagnostic that tells you whether the bottleneck is knockout questions
    # or simply volume. Fast rejections mean a form answer is killing you.
    fast = db.one(
        "SELECT COUNT(*) c FROM applications WHERE outcome='rejection' "
        "AND outcome_at IS NOT NULL AND submitted_at IS NOT NULL "
        "AND julianday(outcome_at)-julianday(submitted_at) < 1")
    total_rej = db.one("SELECT COUNT(*) c FROM applications WHERE outcome='rejection'")
    if total_rej and total_rej["c"]:
        pct = 100.0 * (fast["c"] if fast else 0) / total_rej["c"]
        con.print(f"\n[bold]Fast-reject rate:[/bold] {pct:.0f}% of rejections arrive within 24h.")
        con.print("[dim]High => knockout questions (work auth, grad year) are the bottleneck, "
                  "not your resume.\nLow/silence => volume and timing.[/dim]")



# --------------------------------------------------------------------------- #
# outreach
# --------------------------------------------------------------------------- #
@reach_app.command("harvest")
def outreach_harvest(
    github_org: str = typer.Option(None, "--github-org", help="Public members of a GitHub org."),
    repo: str = typer.Option(None, "--repo", help="Contributors to owner/repo."),
    team_page: str = typer.Option(None, "--team-page", help="A company's public team page URL."),
    csv_file: Path = typer.Option(None, "--csv", help="Names you gathered by hand."),
    limit: int = typer.Option(25, help="Max people per source."),
) -> None:
    """Find people worth contacting. Sources that permit automated access only."""
    settings, profile = _load()
    from .db import get_db
    from .outreach.discover import (ContactStore, domain_pattern, from_csv,
                                    from_github_org, from_github_repo,
                                    from_team_page, synthesize_email)

    db = get_db()
    store = ContactStore(db, settings)
    people = []

    if github_org:
        people += list(from_github_org(github_org, limit=limit))
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        people += list(from_github_repo(owner, name, limit=limit))
    if team_page:
        people += list(from_team_page(team_page, limit=limit))
    if csv_file:
        people += list(from_csv(csv_file))

    if not people:
        con.print("[yellow]No sources given.[/yellow] Try --github-org, --repo, "
                  "--team-page or --csv.")
        con.print("[dim]Tip: ten minutes in your alumni directory, pasted into a CSV, "
                  "beats any scraper and carries no account risk.[/dim]")
        raise typer.Exit(1)

    patterns: dict[str, str | None] = {}
    saved = 0
    for p in people:
        if p.domain and not p.email:
            if p.domain not in patterns:
                patterns[p.domain] = domain_pattern(p.domain, db)
            p.email = synthesize_email(p, patterns[p.domain])
        store.upsert(p)
        saved += 1

    with_hook = sum(1 for p in people if p.hook_source_url)
    con.print(f"[green]{saved}[/green] contacts stored "
              f"([bold]{with_hook}[/bold] with a citable hook).")
    if with_hook < saved:
        con.print(f"[dim]{saved - with_hook} have no hook source and will be skipped when "
                  f"drafting. Add one, or drop them.[/dim]")


@reach_app.command("for-job")
def outreach_for_job(
    target: str = typer.Argument(..., help="A job URL, or the id of a job already stored."),
    limit: int = typer.Option(12, help="How many people to keep."),
) -> None:
    """Find people worth contacting about one posting. Nothing is sent.

    Only sources that state who someone is are used: a contact address the
    posting itself publishes, schema.org Person entries on the company's own
    team page, and public members of a GitHub organisation that corroborates
    the company. Reading names out of arbitrary markup was tried and removed --
    it produced "Blog Trust" and "Chief Executive Officer" as people, and an
    invented recipient is worse than finding nobody.
    """
    settings, _profile = _load()
    from .db import get_db
    from .discover import sources as S
    from .outreach.discover import ContactStore
    from .outreach.forjob import people_for_job, resolve_employer

    db = get_db()
    if target.isdigit():
        row = db.one("SELECT * FROM jobs WHERE id=?", (int(target),))
        if not row:
            con.print(f"[red]no job with id {target}[/red]")
            raise typer.Exit(1)
        job = dict(row)
    else:
        raw = S.fetch_single(target)
        if raw is None:
            con.print(f"[red]could not fetch {target}[/red]")
            raise typer.Exit(1)
        job = dict(db.one("SELECT * FROM jobs WHERE id=?", (db.upsert_job(raw.to_row()),)))

    domain, how = resolve_employer(job, settings, db)
    con.print(f"[bold]{job['company']}[/bold] — {job['title']}")
    con.print(f"[dim]employer domain: {domain or 'not established'} ({how or 'none'})[/dim]")
    if how == "guessed":
        con.print("[yellow]That domain was guessed from the company name and is not "
                  "stated anywhere.[/yellow] Addresses are withheld until you confirm "
                  "it under outreach.company_domains in settings.")

    people = people_for_job(job, settings, db, limit=limit)
    if not people:
        con.print("\n[yellow]Nobody found from a source worth trusting.[/yellow]")
        con.print("[dim]Used: the posting's own contact address, schema.org Person "
                  "entries on the company site, and corroborated GitHub org members. "
                  "A company publishing none of those needs `outreach harvest --csv`, "
                  "which is ten minutes by hand and a better list anyway.[/dim]")
        raise typer.Exit(0)

    store = ContactStore(db, settings)
    t = Table(box=None, pad_edge=False)
    for c in ("name", "role", "reach them at", "why them"):
        t.add_column(c)
    for p in people:
        store.upsert(p)
        t.add_row(p.name, (p.role or "—")[:28],
                  p.email or p.profile_url, (p.hook_fact or "—")[:46])
    con.print(t)
    con.print(f"\n[green]{len(people)}[/green] contact(s) stored. "
              f"`applier outreach draft` writes the messages; nothing is ever sent.")


def _col(row, name: str) -> str:
    """A column that an older row may not have."""
    try:
        value = row[name]
    except (KeyError, IndexError):
        return ""
    return value or ""


@reach_app.command("draft")
def outreach_draft(
    limit: int = typer.Option(4, help="How many to draft (respects the daily cap)."),
) -> None:
    """Write outreach messages. Nothing is sent."""
    settings, profile = _load()
    from .db import get_db
    from .llm import Router
    from .outreach.discover import ContactStore, Person
    from .outreach.draft import (NoChannelError, NoHookError,
                             draft_message, save_batch)

    db = get_db()
    store = ContactStore(db, settings)
    router = Router(settings, profile, db)

    rows = store.pending(limit=limit * 3)
    if not rows:
        con.print("[yellow]No pending contacts.[/yellow] Run `applier outreach harvest` first.")
        raise typer.Exit(1)

    drafts, skipped = [], []
    for row in rows:
        if len(drafts) >= limit:
            break
        p = Person(name=row["name"] or "", company=row["company"] or "",
                   domain=row["domain"] or "", role=row["role"] or "",
                   email=row["email"] or "", source=row["source"] or "",
                   profile_url=_col(row, "profile_url"), github=_col(row, "github"),
                   hook_fact=row["hook_fact"] or "",
                   hook_source_url=row["hook_source_url"] or "")
        ok, why = store.can_contact(p)
        if not ok:
            skipped.append(f"{p.name}: {why}")
            continue
        try:
            drafts.append(draft_message(p, profile, settings, router))
            con.print(f"  [green]drafted[/green] {p.name}")
        except NoHookError as e:
            skipped.append(str(e).split(":", 1)[0] + ": no verifiable hook")
        except NoChannelError as e:
            # Deliberately not folded into the hook message above. These were
            # one exception, so a contact with a perfectly good hook and a
            # dropped channel reported "no verifiable hook" and sent me
            # looking in entirely the wrong place.
            skipped.append(str(e))
        except Exception as e:
            skipped.append(f"{p.name}: {e}")

    if skipped:
        con.print(f"\n[dim]skipped {len(skipped)}:[/dim]")
        for s in skipped[:8]:
            con.print(f"  [dim]- {s}[/dim]")

    if not drafts:
        con.print("\n[yellow]Nothing drafted.[/yellow]")
        raise typer.Exit(1)

    out = data_path(settings.get("outreach.output_dir", "out/outreach"))
    path = save_batch(drafts, out, db)
    con.print(f"\n[bold green]{len(drafts)} draft(s)[/bold green] -> {path}")
    con.print("[dim]Nothing was sent. Read, edit, and send them yourself.[/dim]")


@reach_app.command("list")
def outreach_list() -> None:
    """Contacts and where each one stands."""
    _load()
    from .db import get_db
    rows = get_db().q("SELECT name,company,role,email,stage,hook_source_url FROM contacts "
                      "ORDER BY stage, company LIMIT 100")
    if not rows:
        con.print("[yellow]No contacts yet.[/yellow]")
        return
    t = Table(title="contacts")
    for col in ("Name", "Company", "Role", "Channel", "Stage", "Hook"):
        t.add_column(col, overflow="fold")
    for r in rows:
        t.add_row(r["name"] or "", r["company"] or "", (r["role"] or "")[:22],
                  r["email"] or "-", r["stage"] or "",
                  "[green]yes[/green]" if r["hook_source_url"] else "[red]none[/red]")
    con.print(t)


# --------------------------------------------------------------------------- #
# content bank
# --------------------------------------------------------------------------- #
@bank_app.command("import")
def bank_import(
    master: Path = typer.Option(None, help="Path to master_resume.md."),
) -> None:
    """Rebuild the content bank from your master resume.

    Add to master_resume.md whenever you like, then run this. Numbers are
    extracted into the claims ledger; anything you retired stays retired.
    """
    settings, _ = _load()
    bank_dir = data_path(settings.get("tailor.bank_path", "config/bank"))
    master = master or bank_dir / "master_resume.md"

    from .tailor.master import MasterError, build_bank
    try:
        r = build_bank(master, bank_dir)
    except MasterError as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    con.print(f"[green]imported[/green] {r['entries']} entries -> {r['atoms']} atoms "
              f"({r['pinned']} pinned), {r['skill_groups']} skill groups")
    if r["new_claims"]:
        con.print(f"[yellow]{r['new_claims']} new number(s)[/yellow] added to claims.yaml "
                  f"as [bold]needs_check[/bold].")
    if r["retired_dropped"]:
        con.print(f"[dim]{r['retired_dropped']} retired claim(s) left retired — "
                  f"withdrawn metrics are never resurrected by an import.[/dim]")
    if r["needs_check"]:
        con.print(f"\n[bold]{len(r['needs_check'])} number(s) awaiting your confirmation.[/bold]")
        con.print("[dim]Open config/bank/claims.yaml. For each, ask: could I reconstruct this\n"
                  "out loud, in sixty seconds, under questioning? If yes set status: verified.\n"
                  "If no, set status: RETIRED and it can never appear on a resume again.[/dim]")
        for k in r["needs_check"][:15]:
            con.print(f"   {k}")

    from .tailor.bank import Bank
    problems = Bank(bank_dir).lint()
    con.print(f"\nlint: [{'green' if not problems else 'red'}]{len(problems)} problem(s)"
              f"[/{'green' if not problems else 'red'}]")
    for p in problems[:8]:
        con.print(f"   - {p}")


@bank_app.command("lint")
def bank_lint() -> None:
    """Check every phrasing renders and every number traces to a claim."""
    settings, _ = _load()
    from .tailor.bank import Bank
    b = Bank(data_path(settings.get("tailor.bank_path", "config/bank")))
    problems = b.lint()
    blocking = set(b.blocking_problems())

    retired, unchecked = [], []
    for sect, claims in (b.claims or {}).items():
        if not isinstance(claims, dict):
            continue
        for name, cl in claims.items():
            if not isinstance(cl, dict):
                continue
            if cl.get("status") == "RETIRED":
                retired.append(f"{sect}.{name}")
            elif cl.get("status") == "needs_check":
                unchecked.append(f"{sect}.{name}")

    con.print(f"atoms: {len(b.atoms)}  roles: {len(b.roles)}  "
              f"pinned: {sum(1 for a in b.atoms if a.pinned)}")
    con.print(f"claims: verified/needs_check/retired = "
              f"{sum(1 for s,c in b.claims.items() if isinstance(c,dict) for x in c.values() if isinstance(x,dict) and x.get('status')=='verified')}"
              f"/{len(unchecked)}/{len(retired)}")
    if blocking:
        con.print(f"\n[red]{len(blocking)} problem(s) that stop a resume rendering:[/red]")
        for p in problems:
            if p in blocking:
                con.print(f"   - {p}")
    advisory = [p for p in problems if p not in blocking]
    if advisory:
        # Said separately and said plainly. These used to be reported in the
        # same red block as the rest and refused the render along with them,
        # so a long sentence read as a broken installation.
        con.print(f"\n[yellow]{len(advisory)} problem(s) that make a worse page, "
                  f"not a wrong one — resumes still render:[/yellow]")
        for p in advisory:
            con.print(f"   - {p}")
    if not problems:
        con.print("\n[green]bank is clean — every phrasing renders and every number traces.[/green]")
    if unchecked:
        con.print(f"\n[yellow]{len(unchecked)} number(s) still needs_check:[/yellow] "
                  + ", ".join(unchecked[:10]))


@bank_app.command("preview")
def bank_preview(
    url: str = typer.Argument(None, help="A job URL, or omit and use --text."),
    text: str = typer.Option(None, "--text", help="Paste a job description instead."),
    render: bool = typer.Option(False, "--render", help="Also produce the PDF."),
) -> None:
    """Show exactly what a given job would select, before applying."""
    settings, profile = _load()
    from .tailor.bank import Bank, job_family

    jd, title, company = text or "", "preview", "preview"
    if url and not text:
        from .discover import sources as S
        raw = S.fetch_single(url)
        if raw is None:
            con.print(f"[red]could not fetch {url}[/red]")
            raise typer.Exit(1)
        jd, title, company = raw.description, raw.title, raw.company
    if not jd:
        con.print("[yellow]give a URL or --text[/yellow]")
        raise typer.Exit(1)

    bank_dir = data_path(settings.get("tailor.bank_path", "config/bank"))
    b = Bank(bank_dir)
    fam = job_family({"title": title, "description": jd})
    sel = b.select(jd, family=fam, line_budget=38)

    con.print(f"[bold]{company}[/bold] — {title}")
    con.print(f"job family: [cyan]{fam}[/cyan]   "
              f"selected {len(sel.atoms)}/{len(b.atoms)} atoms\n")

    by_section: dict = {}
    for atom, size in sel.atoms:
        by_section.setdefault(atom.section, []).append((atom, size))
    for section in ("education", "experience", "projects", "leadership"):
        rows = by_section.get(section)
        if not rows:
            continue
        con.print(f"[bold]{section.upper()}[/bold]")
        for atom, size in rows:
            pin = "[green]PIN[/green] " if atom.pinned else "    "
            body, _ = b.resolve(atom.text(size), strict=False)
            con.print(f"  {pin}[dim]{size:6}[/dim] {body[:104]}")
        con.print("")

    if sel.pinned_dropped:
        con.print(f"[red]pins that did not fit:[/red] {', '.join(sel.pinned_dropped)}")
        con.print("[dim]Your non-negotiables exceed one page. Unpin something.[/dim]")
    if sel.gaps:
        con.print(f"[yellow]gaps[/yellow] — this job asks for, and your bank cannot back: "
                  f"{', '.join(sel.gaps)}")
        con.print("[dim]Deliberately left off the resume. This is the input to what to learn next.[/dim]")

    if render:
        from .tailor.pipeline import build_resume
        art = Path("out/preview")
        art.mkdir(parents=True, exist_ok=True)
        pdf, rep, _ = build_resume(
            {"company": company, "title": title, "description": jd, "url": url or ""},
            settings, profile, art)
        con.print(f"\n[green]{rep}[/green] -> {pdf}")

# --------------------------------------------------------------------------- #
@config_app.command("set-key")
def set_key(name: str = typer.Argument(..., help="e.g. GEMINI_API_KEY")) -> None:
    """Store a secret in the OS keychain."""
    val = getpass.getpass(f"{name} (hidden): ").strip()
    if not val:
        con.print("[yellow]nothing entered[/yellow]"); raise typer.Exit(1)
    set_secret(name, val)
    con.print(f"[green]stored[/green] {name}")


@config_app.command("show")
def config_show() -> None:
    """Print effective settings with secrets masked."""
    from .config import redact
    import yaml as _y
    settings, _ = _load()
    con.print(_y.safe_dump(redact(settings.raw), sort_keys=False, allow_unicode=True))


@config_app.command("models")
def config_models(
    set_primary: str = typer.Option(
        None, "--primary", metavar="PROVIDER/MODEL",
        help="Switch the primary model, e.g. openai/gpt-5 or gemini/gemini-3.5-flash."),
    base_url: str = typer.Option(
        None, "--base-url",
        help="Endpoint for the primary, required for provider 'openai_compatible'."),
    key_env: str = typer.Option(
        None, "--key-env", help="Environment variable holding the primary's API key."),
    task: str = typer.Option(
        None, "--task", help="Task to override, e.g. cover_letter."),
    task_model: str = typer.Option(
        None, "--task-model", help="Model for --task, or 'primary' to clear it."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show or change the model chain: primary, fallbacks and per-task overrides.

    With no options it prints the chain. API keys are never shown or accepted
    here -- only the NAME of the environment variable holding one, and whether
    that name currently resolves. Use `applier config set-key` for the secret
    itself.
    """
    from .web import api as _api

    updates: dict = {}
    if set_primary or base_url or key_env:
        current = dict(_api.get_models()["primary"])
        current.pop("api_key_set", None)
        if set_primary:
            if "/" not in set_primary:
                con.print("[red]--primary must be PROVIDER/MODEL[/red], "
                          "e.g. openai/gpt-5")
                raise typer.Exit(1)
            provider, _, model = set_primary.partition("/")
            current["provider"], current["model"] = provider.strip(), model.strip()
        if base_url:
            current["base_url"] = base_url.strip()
        if key_env:
            current["api_key_env"] = key_env.strip()
        updates["primary"] = current

    if task or task_model:
        if not (task and task_model):
            con.print("[red]--task and --task-model go together[/red]")
            raise typer.Exit(1)
        tasks = dict(_api.get_models()["task_models"])
        tasks[task] = task_model
        updates["task_models"] = tasks

    try:
        data = _api.put_models(updates) if updates else _api.get_models()
    except Exception as e:
        con.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if json_out:
        import json as _j
        con.print_json(_j.dumps({k: v for k, v in data.items() if k != "ok"}))
        return

    if updates:
        con.print("[green]saved[/green]\n")

    t = Table(title="model chain", box=None, pad_edge=False)
    for col in ("#", "provider", "model", "key env", "key", "endpoint"):
        t.add_column(col)
    chain = [("primary", data["primary"])] + [
        (str(i + 1), f) for i, f in enumerate(data["fallbacks"])]
    for label, spec in chain:
        t.add_row(label, str(spec.get("provider", "")), str(spec.get("model", "")),
                  str(spec.get("api_key_env", "")),
                  "[green]set[/green]" if spec.get("api_key_set") else "[red]unset[/red]",
                  str(spec.get("base_url", "") or "default"))
    con.print(t)

    overrides = {k: v for k, v in (data.get("task_models") or {}).items()
                 if v != "primary"}
    if overrides:
        con.print("\n[bold]per-task overrides[/bold]")
        for k, v in overrides.items():
            con.print(f"  {k}: {v}")
    con.print(f"\n[dim]providers: {', '.join(data['providers'])}[/dim]")
    con.print("[dim]Any vendor with an OpenAI-compatible endpoint works without a "
              "code change: provider 'openai_compatible' plus --base-url.[/dim]")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        con.print("\n[yellow]interrupted[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
