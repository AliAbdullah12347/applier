"""Finding people worth contacting, and how to reach them.

Referrals convert roughly ten times better than cold applications, which makes
this module worth more per hour than anything else in the system. It is also the
one place where the obvious approach is the wrong one.

**Why there is no LinkedIn scraping here.** LinkedIn's User Agreement prohibits
automated access outright, and enforcement is account-level. For someone whose
network *is* the asset, losing the account costs far more than any number of
harvested names. So LinkedIn is used the way a person uses it — by hand — and
this module automates everything around it:

    harvest   public sources that permit it: GitHub org members and repo
              contributors, company team/engineering pages, conference speaker
              lists, paper author lists, plus a CSV you paste names into after
              ten minutes of manual browsing
    enrich    infer the corporate email pattern once per company, then
              synthesise addresses locally
    hook      find a real, citable reason to contact this specific person
    draft     write the message (see draft.py)

Nothing is ever sent. The system produces a person, a channel, and a draft; the
send is yours.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import httpx

from ..config import Config, get_secret
from ..db import Database, now

UA = "applier-outreach/0.1 (personal job-search tool)"
GITHUB_API = "https://api.github.com"

# Patterns corporate mail follows, in rough order of prevalence.
EMAIL_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}@{domain}",
    "{f}{last}@{domain}",
    "{first}{last}@{domain}",
    "{first}_{last}@{domain}",
    "{last}{f}@{domain}",
    "{f}.{last}@{domain}",
]


@dataclass
class Person:
    name: str
    company: str = ""
    domain: str = ""
    role: str = ""
    email: str = ""
    profile_url: str = ""
    github: str = ""
    source: str = ""
    hook_fact: str = ""
    hook_source_url: str = ""
    notes: str = ""

    @property
    def key(self) -> str:
        return f"{re.sub(r'[^a-z]', '', self.name.lower())}@{self.domain.lower()}"

    @property
    def contactable(self) -> bool:
        return bool(self.email or self.profile_url)


def _client(token: str | None = None) -> httpx.Client:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=30.0, follow_redirects=True)


# --------------------------------------------------------------------------- #
# harvesting
# --------------------------------------------------------------------------- #
def from_github_org(org: str, *, limit: int = 30) -> Iterator[Person]:
    """Public members of a GitHub organisation.

    Only returns people who have *chosen* to make their membership and profile
    public. A public commit email is a published contact address.
    """
    token = get_secret("GITHUB_TOKEN")
    with _client(token) as c:
        r = c.get(f"{GITHUB_API}/orgs/{org}/members", params={"per_page": min(limit, 100)})
        if r.status_code != 200:
            return
        members = r.json()
        for m in members[:limit]:
            login = m.get("login")
            if not login:
                continue
            pr = c.get(f"{GITHUB_API}/users/{login}")
            if pr.status_code != 200:
                continue
            u = pr.json()
            yield Person(
                name=u.get("name") or login,
                company=(u.get("company") or org).lstrip("@"),
                role="engineer",
                email=u.get("email") or "",
                profile_url=u.get("html_url", ""),
                github=login,
                source="github_org",
                hook_fact=f"maintains or contributes to {org} on GitHub",
                hook_source_url=u.get("html_url", ""),
                notes=u.get("bio") or "",
            )


def from_github_repo(owner: str, repo: str, *, limit: int = 20) -> Iterator[Person]:
    """Top contributors to a specific repository.

    This is the highest-quality hook available anywhere: you can open their
    actual code, read it, and say something true and specific about it.
    """
    token = get_secret("GITHUB_TOKEN")
    with _client(token) as c:
        r = c.get(f"{GITHUB_API}/repos/{owner}/{repo}/contributors",
                  params={"per_page": min(limit, 100)})
        if r.status_code != 200:
            return
        for con in r.json()[:limit]:
            login = con.get("login")
            if not login or login.endswith("[bot]"):
                continue
            pr = c.get(f"{GITHUB_API}/users/{login}")
            if pr.status_code != 200:
                continue
            u = pr.json()
            yield Person(
                name=u.get("name") or login,
                company=(u.get("company") or "").lstrip("@"),
                role="engineer",
                email=u.get("email") or "",
                profile_url=u.get("html_url", ""),
                github=login,
                source="github_repo",
                hook_fact=f"has {con.get('contributions', 0)} commits in {owner}/{repo}",
                hook_source_url=f"https://github.com/{owner}/{repo}/commits?author={login}",
                notes=u.get("bio") or "",
            )


TEAM_PAGE_HINTS = ["/team", "/about", "/people", "/company/team", "/leadership", "/engineering"]
NAME_RE = re.compile(r"\b([A-Z][a-z]{1,15})\s+([A-Z][a-z']{1,20})\b")
ROLE_NEAR_RE = re.compile(
    r"(engineer|scientist|researcher|recruiter|manager|lead|director|founder|analyst|"
    r"developer|architect|head of [a-z ]{3,20})", re.I)


def from_team_page(url: str, *, limit: int = 25) -> Iterator[Person]:
    """Scrape a company's own public team page.

    Publishing a team page is an invitation to read it. This only reads what the
    company chose to publish, at one request per company.
    """
    from ..discover.sources import _strip_html, domain_from_url

    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=30.0, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError:
        return

    text = _strip_html(html)
    domain = domain_from_url(url)
    seen: set[str] = set()
    for m in NAME_RE.finditer(text):
        name = f"{m.group(1)} {m.group(2)}"
        if name.lower() in seen:
            continue
        window = text[max(0, m.start() - 90): m.end() + 90]
        rm = ROLE_NEAR_RE.search(window)
        if not rm:
            continue                      # a bare capitalised pair is probably not a person
        seen.add(name.lower())
        yield Person(
            name=name, company=domain.split(".")[0].title(), domain=domain,
            role=rm.group(0).lower(), profile_url=url, source="team_page",
            hook_fact=f"listed as {rm.group(0).lower()} on the team page",
            hook_source_url=url,
        )
        if len(seen) >= limit:
            return


def from_csv(path: Path) -> Iterator[Person]:
    """Import names you gathered by hand.

    This is the deliberate manual step. Ten minutes in your university's alumni
    directory or on LinkedIn, by hand, produces a better target list than any
    scraper — and carries no account risk. Columns: name, company, domain, role,
    profile_url, hook_fact, hook_source_url (all optional except name).
    """
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            yield Person(
                name=name,
                company=(row.get("company") or "").strip(),
                domain=(row.get("domain") or "").strip().lower(),
                role=(row.get("role") or "").strip(),
                email=(row.get("email") or "").strip(),
                profile_url=(row.get("profile_url") or "").strip(),
                source="manual_csv",
                hook_fact=(row.get("hook_fact") or "").strip(),
                hook_source_url=(row.get("hook_source_url") or "").strip(),
                notes=(row.get("notes") or "").strip(),
            )


# --------------------------------------------------------------------------- #
# enrichment
# --------------------------------------------------------------------------- #
def domain_pattern(domain: str, db: Database) -> str | None:
    """One Hunter.io lookup per COMPANY, cached forever.

    Hunter's free tier is 50 credits/month. Spending one per person burns it in a
    day; spending one per company and synthesising the rest locally makes it last
    all season.
    """
    row = db.one("SELECT email FROM contacts WHERE domain=? AND email LIKE '%@%' LIMIT 1",
                 (domain,))
    if row and row["email"]:
        inferred = _infer_pattern(row["email"], domain)
        if inferred:
            return inferred

    key = get_secret("HUNTER_API_KEY")
    if not key:
        return None
    try:
        with httpx.Client(timeout=30.0) as c:
            r = c.get("https://api.hunter.io/v2/domain-search",
                      params={"domain": domain, "department": "engineering", "api_key": key})
            if r.status_code != 200:
                return None
            return (r.json().get("data") or {}).get("pattern")
    except httpx.HTTPError:
        return None


def _infer_pattern(email: str, domain: str) -> str | None:
    local = email.split("@")[0].lower()
    if "." in local:
        return "{first}.{last}"
    if "_" in local:
        return "{first}_{last}"
    return None


def synthesize_email(person: Person, pattern: str | None) -> str:
    """Build an address locally from a known company pattern."""
    if person.email:
        return person.email
    if not pattern or not person.domain:
        return ""
    parts = person.name.lower().replace("-", " ").split()
    if len(parts) < 2:
        return ""
    first, last = re.sub(r"[^a-z]", "", parts[0]), re.sub(r"[^a-z]", "", parts[-1])
    if not first or not last:
        return ""
    tmpl = pattern.replace("{f}", "{first_initial}")
    return (tmpl.format(first=first, last=last, first_initial=first[0],
                        domain=person.domain) if "@" in tmpl
            else f"{tmpl.format(first=first, last=last, first_initial=first[0])}@{person.domain}")


def channel_for(person: Person) -> tuple[str, str]:
    """How to actually reach this person, and via what.

    Returns (channel, target). Ranked by reply rate, not convenience.
    """
    if person.email:
        return "email", person.email
    if person.github:
        return "github", f"https://github.com/{person.github}"
    if person.profile_url:
        return "profile", person.profile_url
    return "unknown", ""


# --------------------------------------------------------------------------- #
# persistence + rate limits
# --------------------------------------------------------------------------- #
class ContactStore:
    """Rate limits live here, enforced in code rather than in a guideline.

    Outreach that becomes spam does not just fail — it attaches the sender's real
    name to it. So the caps are structural: the drafter refuses to generate beyond
    them rather than warning and continuing.
    """

    def __init__(self, db: Database, settings: Config) -> None:
        self.db = db
        self.s = settings
        oc = settings.get("outreach", {}) or {}
        self.max_per_day = int(oc.get("max_per_day", 4))
        self.max_per_company_week = int(oc.get("max_per_company_per_week", 2))
        self.max_dead_per_company = int(oc.get("max_lifetime_per_company_no_reply", 3))

    def upsert(self, p: Person) -> int:
        self.db.run(
            "INSERT INTO contacts(person_key,name,company,domain,email,role,source,"
            "hook_fact,hook_source_url,stage) VALUES(?,?,?,?,?,?,?,?,?,'harvested') "
            "ON CONFLICT(person_key) DO UPDATE SET "
            "email=COALESCE(NULLIF(excluded.email,''),contacts.email),"
            "hook_fact=COALESCE(NULLIF(excluded.hook_fact,''),contacts.hook_fact),"
            "hook_source_url=COALESCE(NULLIF(excluded.hook_source_url,''),contacts.hook_source_url)",
            (p.key, p.name, p.company, p.domain, p.email, p.role, p.source,
             p.hook_fact, p.hook_source_url),
        )
        row = self.db.one("SELECT id FROM contacts WHERE person_key=?", (p.key,))
        return int(row["id"]) if row else 0

    def sent_today(self) -> int:
        r = self.db.one("SELECT COUNT(*) c FROM contacts WHERE date(last_contact)=date('now')")
        return int(r["c"]) if r else 0

    def can_contact(self, p: Person) -> tuple[bool, str]:
        if self.sent_today() >= self.max_per_day:
            return False, f"daily cap reached ({self.max_per_day})"
        r = self.db.one(
            "SELECT COUNT(*) c FROM contacts WHERE domain=? AND last_contact IS NOT NULL "
            "AND julianday('now')-julianday(last_contact) < 7", (p.domain,))
        if r and int(r["c"]) >= self.max_per_company_week:
            return False, f"{p.domain}: {self.max_per_company_week} contacted in the last 7 days"
        r = self.db.one(
            "SELECT COUNT(*) c FROM contacts WHERE domain=? AND stage IN ('sent','followed_up')",
            (p.domain,))
        if r and int(r["c"]) >= self.max_dead_per_company:
            return False, f"{p.domain}: {self.max_dead_per_company} contacted with no reply"
        return True, ""

    def mark_drafted(self, p: Person) -> None:
        self.db.run("UPDATE contacts SET stage='drafted' WHERE person_key=?", (p.key,))

    def pending(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT * FROM contacts WHERE stage IN ('harvested','drafted') "
            "ORDER BY (hook_source_url IS NOT NULL AND hook_source_url != '') DESC, id "
            "LIMIT ?", (limit,))]
