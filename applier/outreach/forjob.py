"""People worth contacting about one specific posting.

`harvest` already finds people, but it has to be told where to look: a GitHub
org, a team page, a CSV. That is fine when you have picked a company and are
willing to go and find its engineering page by hand. It is useless as the
answer to "here is a job link, who should I talk to", which is the question
that actually comes up.

The hard part is not finding people. It is that a posting does not tell you
who the employer *is*, in the sense a crawler needs. Every Greenhouse listing
carries the host `job-boards.greenhouse.io`; the board slug is a marketing
decision ("alphasightsresumedrop"); and the description rarely names the
company's own domain in a way worth trusting. So this module spends most of
its effort establishing the employer's domain, and only then looks for people
on it.

Everything here reads pages the company chose to publish, at a handful of
requests per company, and caches the expensive answer. Nothing is sent --
`draft.py` writes the message and the send is always yours.
"""

from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import urlparse

from ..config import Config
from ..db import Database
from ..net import guarded_get, guarded_text
from .discover import (UA, Person, domain_pattern, from_github_org,
                       synthesize_email)

# Hosts that belong to an applicant tracking system or an aggregator rather
# than to the employer. A posting's URL host is only the employer's domain
# when it is none of these.
ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "workable.com", "myworkdayjobs.com", "workday.com", "icims.com",
    "jobvite.com", "bamboohr.com", "breezy.hr", "recruitee.com",
    "teamtailor.com", "personio.de", "pinpointhq.com", "rippling.com",
    "paylocity.com", "dayforcehcm.com", "successfactors.com", "taleo.net",
    "eightfold.ai", "phenompeople.com", "oraclecloud.com", "adp.com",
    "simplify.jobs", "indeed.com", "linkedin.com", "glassdoor.com",
    "ziprecruiter.com", "builtin.com", "wellfound.com", "angel.co",
    "google.com/about/careers", "myworkdaysite.com", "jobs.cz",
)

# Paths companies conventionally publish their people on.
TEAM_PATHS = ("/team", "/about", "/about-us", "/company/team", "/people",
              "/leadership", "/our-team", "/company", "/about/team")

# Addresses that exist to absorb mail, not to be read by a person.
DEAD_LOCALS = ("noreply", "no-reply", "donotreply", "do-not-reply", "privacy",
               "legal", "abuse", "postmaster", "webmaster", "security",
               "support", "info", "hello", "contact", "sales", "press",
               "accessibility", "compliance", "unsubscribe", "notifications")

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

# Roles worth a message, by what the posting is for. The first list that the
# title matches decides the ordering; `None` is the fallback.
ROLE_PRIORITY: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("intern", "campus", "university", "new grad", "graduate"),
     ("campus", "university", "early careers", "recruit", "talent",
      "people", "hiring")),
    (("research", "scientist", "phd"),
     ("research", "scientist", "principal", "staff")),
    ((),  # everything else: engineers first, then whoever hires them
     ("engineer", "engineering", "developer", "technical", "architect",
      "cto", "vp", "head", "lead", "manager", "recruit", "talent")),
]

MAX_PAGE_FETCHES = 6          # per job, total. Politeness is a budget.
TIMEOUT = 20.0


def _get(url: str) -> str:
    """Fetch a URL that came out of a job description.

    Job descriptions are written by whoever posted the job, so the links in
    them are attacker-controlled. Everything here goes through the guard in
    applier.net, which refuses loopback, private and link-local addresses
    after DNS resolution and re-checks on every redirect.
    """
    return guarded_text(url, headers={"User-Agent": UA}, timeout=TIMEOUT)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# Subdomains a company puts its careers site on. The employer is the parent --
# team pages and mail both live there, not under jobs.*.
_CAREERS_SUBS = ("jobs", "careers", "career", "apply", "work", "join",
                 "hiring", "talent", "recruiting", "www", "boards", "job")


def _registrable(host: str) -> str:
    """Drop a careers subdomain so "jobs.bytedance.com" resolves to the employer."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    while len(parts) > 2 and parts[0] in _CAREERS_SUBS:
        parts = parts[1:]
    return ".".join(parts)


def is_ats_host(host: str) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) or h in host for h in ATS_HOSTS)


# --------------------------------------------------------------------------- #
# whose domain is this, really
# --------------------------------------------------------------------------- #
def _cached_domain(db: Database | None, company: str) -> str | None:
    if db is None:
        return None
    row = db.one("SELECT value FROM meta WHERE key=?", (f"employer_domain:{_slug(company)}",))
    return row["value"] if row else None


def _cache_domain(db: Database | None, company: str, domain: str) -> None:
    if db is None:
        return
    db.run("INSERT INTO meta(key,value) VALUES(?,?) "
           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
           (f"employer_domain:{_slug(company)}", domain))


def _domains_in(text: str) -> list[str]:
    """Non-ATS hosts a posting mentions, most frequent first."""
    seen: dict[str, int] = {}
    for m in re.finditer(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})", text or ""):
        host = m.group(1).lower().removeprefix("www.")
        if not is_ats_host(host):
            seen[host] = seen.get(host, 0) + 1
    for m in EMAIL_RE.finditer(text or ""):
        host = m.group(1).lower().removeprefix("www.")
        if not is_ats_host(host):
            seen[host] = seen.get(host, 0) + 3        # an address is a strong signal
    return [h for h, _ in sorted(seen.items(), key=lambda kv: -kv[1])]


def _strip_host(text: str, host: str) -> str:
    """Remove the domain's own name from a page before reading it as evidence.

    A page titled "XAI.COM - CTR LIMITED" contains the string "xai", so any
    check that reads the raw text concludes the site belongs to xAI. It does
    not; it belongs to a company called CTR Limited that happens to hold the
    domain. Every domain-holder page in the world passes that test, which is
    the one population this check exists to exclude.
    """
    out = re.sub(re.escape(host), " ", text or "", flags=re.I)
    stem = host.split(".")[0]
    if len(stem) > 2:
        out = re.sub(rf"\b{re.escape(stem)}\s*\.\s*[a-z]{{2,6}}\b", " ", out, flags=re.I)
    return out


def _site_identity(html: str) -> str:
    """What the page says it is: its title and its declared site name."""
    parts = []
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if m:
        parts.append(m.group(1))
    for prop in ("og:site_name", "application-name", "og:title"):
        m = re.search(rf"""["']{re.escape(prop)}["'][^>]*?content=["']([^"']{{1,200}})""",
                      html or "", re.I)
        if m:
            parts.append(m.group(1))
    return " ".join(parts)


def _looks_like(company: str, html: str, host: str, *, strict: bool) -> bool:
    """Does this site belong to this company?

    `strict` is for a domain nobody pointed us at -- one guessed from the
    company's name. There the only acceptable evidence is the site declaring
    itself: its <title> or og:site_name. Body copy is not enough, because a
    parking page's body copy is whatever its owner is advertising, and two
    of the first ten real postings tested resolved to exactly that.

    When the posting itself named the domain, a mention in the body is
    sufficient -- something already connected the two.
    """
    name = _slug(company)
    if not name or len(name) < 3:
        return False

    if strict:
        identity = _slug(_strip_host(_site_identity(html), host))
        return bool(identity) and name in identity

    body = _slug(_strip_host((html or "")[:60_000], host))
    return name in body


def resolve_employer(job: dict[str, Any], settings: Config | None = None,
                     db: Database | None = None) -> tuple[str, str]:
    """The employer's domain and how it was established.

    Returns (domain, source) where source is one of:

        override   you told it, in outreach.company_domains
        posting    the posting's own host, which was not an ATS
        described  a domain the description names, confirmed by the page
        guessed    the company name plus a TLD, confirmed only by the page
                   declaring itself
        ""         nothing credible

    The distinction matters more than it looks. Testing the first ten real
    postings, two of the ten `guessed` answers were wrong: "xAI" resolved to
    xai.com, which belongs to a company called CTR Limited that holds the
    domain, and would have produced a list of strangers to email in Ali's
    name. Reading the wrong website costs nothing. Writing to the people on
    it is the one irreversible thing this module can do, so a guessed domain
    is never allowed to produce a contactable person.
    """
    company = str(job.get("company") or "")

    # 1. an explicit mapping, because some companies are simply unguessable
    if settings is not None:
        override = (settings.get("outreach.company_domains", {}) or {}).get(company)
        if override:
            return str(override).lower(), "override"

    cached = _cached_domain(db, company)
    if cached is not None:
        domain, _, how = cached.partition("|")
        return domain, (how or "cached")

    resolved = ""
    how = ""

    # 2. the posting's own host, when it is not an ATS
    host = _registrable(
        (urlparse(str(job.get("url") or "")).hostname or "").lower())
    if host and not is_ats_host(host):
        resolved, how = host, "posting"

    # 3. a domain the description mentions
    if not resolved:
        for candidate in _domains_in(str(job.get("description") or ""))[:3]:
            page = _get(f"https://{candidate}")
            if page and _looks_like(company, page, candidate, strict=False):
                resolved, how = candidate, "described"
                break

    # 4. the company name as a domain, verified before it is believed
    if not resolved:
        name = _slug(company)
        if 2 < len(name) <= 30:
            for tld in (".com", ".io", ".ai", ".co"):
                candidate = name + tld
                page = _get(f"https://{candidate}")
                if page and _looks_like(company, page, candidate, strict=True):
                    resolved, how = candidate, "guessed"
                    break

    _cache_domain(db, company, f"{resolved}|{how}")
    return resolved, how


def employer_domain(job: dict[str, Any], settings: Config | None = None,
                    db: Database | None = None) -> str:
    """Just the domain. See resolve_employer for how it was established."""
    return resolve_employer(job, settings, db)[0]


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #
def from_posting(job: dict[str, Any], domain: str) -> Iterator[Person]:
    """A named contact the posting itself gives you.

    Rare, but when a listing says "questions to jane.doe@example.com" that is
    the single best address in the entire pipeline: the employer published it
    for exactly this purpose.
    """
    text = str(job.get("description") or "")
    for m in EMAIL_RE.finditer(text):
        addr = m.group(0).lower()
        local, _, host = addr.partition("@")
        if any(d in local for d in DEAD_LOCALS):
            continue
        if domain and host.removeprefix("www.") != domain:
            continue
        pretty = re.sub(r"[._-]+", " ", local).title()
        if len(pretty.split()) < 2:
            continue                      # a role address, not a person
        yield Person(
            name=pretty, company=str(job.get("company") or ""), domain=host,
            email=addr, role="named in the posting", source="posting",
            profile_url=str(job.get("url") or ""),
            hook_fact=f"listed as the contact on the {job.get('title')} posting",
            hook_source_url=str(job.get("url") or ""),
        )


def from_company_site(domain: str, company: str = "", *, limit: int = 20,
                      budget: int = MAX_PAGE_FETCHES,
                      render: bool = True) -> Iterator[Person]:
    """People a company publishes about itself, from structured data.

    Tries the conventional paths and stops at the first that yields anyone.
    A page with no schema.org markup yields nobody, by design -- see
    people.people_on_page for why guessing is not offered here.
    """
    from .people import fetch_rendered, people_on_page

    found = 0
    for path in TEAM_PATHS:
        if budget <= 0 or found >= limit:
            return
        budget -= 1
        url = f"https://{domain}{path}"
        html = _get(url)
        if not html:
            continue

        rows = people_on_page(html, limit=limit - found)
        if not rows and render:
            # The page may build itself in the browser. One render attempt per
            # path that returned HTML but no structured people.
            rows = people_on_page(fetch_rendered(url), limit=limit - found)

        for name, role in rows:
            found += 1
            yield Person(
                name=name, company=company or domain.split(".")[0].title(),
                domain=domain, role=role.lower(), profile_url=url,
                source="company_site",
                hook_fact=f"listed as {role.lower()} on {domain}{path}" if role
                          else f"listed on {domain}{path}",
                hook_source_url=url,
            )
        if found:
            return


def from_company_github(company: str, domain: str, *, limit: int = 15) -> Iterator[Person]:
    """Public members of a GitHub org that is plausibly this company's.

    Verified rather than guessed: the org's own name or blog has to point back
    at the company, or an unrelated org with a similar slug ends up on the list.
    """
    # Companies name their org after the short form: "Wikimedia Foundation"
    # is `wikimedia`, "AiDash Inc" is `aidash`. Try the obvious variants, but
    # believe none of them until the org points back at the company.
    base = _slug(company)
    stem = _slug(re.split(r"\s+", company.strip())[0]) if company.strip() else ""
    variants = [v for v in dict.fromkeys([base, stem, _slug(domain.split(".")[0])])
                if v and 2 < len(v) <= 39]

    for slug in variants:
        response = guarded_get(f"https://api.github.com/orgs/{slug}",
                               headers={"User-Agent": UA}, timeout=TIMEOUT)
        if response is None or response.status_code != 200:
            continue
        try:
            meta = response.json()
        except ValueError:
            continue

        blog = _slug(str(meta.get("blog") or ""))
        org_name = _slug(str(meta.get("name") or ""))
        # Corroboration, not coincidence: either the org's own name matches the
        # company, or its website is the domain we already established. A
        # GitHub org whose slug merely resembles a company name is somebody
        # else's project.
        if not (org_name and (org_name == base or org_name == stem
                              or base in org_name or org_name in base))                 and not (domain and _slug(domain) in blog):
            continue
        yield from from_github_org(slug, limit=limit)
        return


def rank(people: list[Person], job: dict[str, Any]) -> list[Person]:
    """Most worth writing to first, for this particular posting."""
    title = str(job.get("title") or "").lower()
    order: tuple[str, ...] = ROLE_PRIORITY[-1][1]
    for triggers, roles in ROLE_PRIORITY:
        if triggers and any(t in title for t in triggers):
            order = roles
            break

    def key(p: Person) -> tuple:
        role = (p.role or "").lower()
        rank_i = next((i for i, word in enumerate(order) if word in role), len(order))
        return (
            0 if p.source == "posting" else 1,   # a published contact always wins
            rank_i,
            0 if p.email else 1,
            0 if p.hook_fact else 1,
            p.name.lower(),
        )

    return sorted(people, key=key)


def people_for_job(job: dict[str, Any], settings: Config | None = None,
                   db: Database | None = None, *, limit: int = 12) -> list[Person]:
    """Everyone worth contacting about this posting, best first.

    Never raises. Outreach is an enhancement to an application, and a company
    with no public team page should cost you a thinner list, not a crash in
    the middle of an unattended run.
    """
    company = str(job.get("company") or "")
    try:
        domain, how = resolve_employer(job, settings, db)
    except Exception:
        domain, how = "", ""

    # A guessed domain may be read but may not be written to. If this is the
    # right company you will see it in the list and can confirm it once, in
    # settings, under outreach.company_domains -- after which it is an
    # override and behaves normally.
    trusted = how in ("override", "posting", "described")

    people: list[Person] = []
    seen: set[str] = set()

    def add(it: Iterator[Person]) -> None:
        try:
            for p in it:
                if not p.name or p.key in seen:
                    continue
                seen.add(p.key)
                if not p.company:
                    p.company = company
                people.append(p)
                if len(people) >= limit * 2:
                    return
        except Exception:
            return

    if domain:
        add(from_posting(job, domain))
        add(from_company_site(domain, company, limit=limit))
        add(from_company_github(company, domain, limit=limit))

    if db is not None and domain and trusted:
        try:
            pattern = domain_pattern(domain, db)
        except Exception:
            pattern = None
        if pattern:
            for p in people:
                if not p.email:
                    p.email = synthesize_email(p, pattern)

    if not trusted:
        for p in people:
            # Keep the person, drop the address. The draft step refuses a
            # contact with no channel, so nothing can be written to them until
            # the domain is confirmed by hand.
            p.email = ""
            p.notes = (p.notes + " " if p.notes else "") + (
                f"[unconfirmed: {domain} was guessed from the company name, not "
                f"stated anywhere. Confirm it in outreach.company_domains before "
                f"contacting anyone here.]")

    return rank([p for p in people if p.contactable], job)[:limit]
