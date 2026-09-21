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

from .config import (
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
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill everything, stop before submit."),
    review: bool = typer.Option(False, "--review", help="Open the filled form for you to check."),
) -> None:
    """One-shot: give it a link, it does the rest."""
    settings, profile = _load()
    if dry_run:
        settings.set("apply.mode", "dry_run")
    elif review:
        settings.set("apply.mode", "review")

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
def run(
    max_apps: int = typer.Option(0, help="Stop after N applications (0 = use settings cap)."),
    once: bool = typer.Option(False, "--once", help="One cycle then exit."),
) -> None:
    """Autonomous loop: discover, rank, tailor, apply. Leave it running."""
    settings, profile = _load()
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


@reach_app.command("draft")
def outreach_draft(
    limit: int = typer.Option(4, help="How many to draft (respects the daily cap)."),
) -> None:
    """Write outreach messages. Nothing is sent."""
    settings, profile = _load()
    from .db import get_db
    from .llm import Router
    from .outreach.discover import ContactStore, Person
    from .outreach.draft import NoHookError, draft_message, save_batch

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
        except Exception as e:
            skipped.append(f"{p.name}: {e}")

    if skipped:
        con.print(f"\n[dim]skipped {len(skipped)}:[/dim]")
        for s in skipped[:8]:
            con.print(f"  [dim]- {s}[/dim]")

    if not drafts:
        con.print("\n[yellow]Nothing drafted.[/yellow]")
        raise typer.Exit(1)

    out = Path(settings.get("outreach.output_dir", "out/outreach"))
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
    bank_dir = Path(settings.get("tailor.bank_path", "config/bank"))
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
    b = Bank(Path(settings.get("tailor.bank_path", "config/bank")))
    problems = b.lint()

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
    if problems:
        con.print(f"\n[red]{len(problems)} problem(s):[/red]")
        for p in problems:
            con.print(f"   - {p}")
    else:
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

    bank_dir = Path(settings.get("tailor.bank_path", "config/bank"))
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


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        con.print("\n[yellow]interrupted[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
