"""Job discovery from free, unauthenticated sources.

Two kinds of source:

* **Aggregate feeds** (SimplifyJobs, zshah tracker) — breadth. These are also how
  we discover which companies use which ATS, by mining apply URLs for board
  tokens. That one step yields hundreds of Greenhouse/Lever/Ashby slugs for free.
* **Direct ATS boards** — freshness and the full job description. Public JSON
  endpoints, no auth, no key.

Deliberately absent: LinkedIn, Indeed, Glassdoor, Handshake. Scraping them
violates their terms, and for this candidate a LinkedIn restriction would cost
the referral channel, which converts roughly ten times better than cold applying.

Workday is not polled directly either: it sits behind Akamai bot management that
rate-limits by source IP across all tenants at once, so one block takes out a
thousand employers.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

UA = "applier/0.1 (personal job-search tool)"
TIMEOUT = 45.0

# --------------------------------------------------------------------------- #
BOARD_PATTERNS = {
    "greenhouse": re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    "lever": re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)"),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)"),
    "smartrecruiters": re.compile(r"careers\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    "workable": re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    "recruitee": re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com"),
}

COUNTRY_HINTS = [
    ("US", ["united states", " usa", ", ny", ", ca", ", wa", ", tx", ", ma", "new york",
            "san francisco", "seattle", "boston", "austin", "chicago", "remote - us"]),
    ("UK", ["united kingdom", "london", "cambridge, uk", "manchester", "edinburgh"]),
    ("DE", ["germany", "berlin", "munich", "münchen", "hamburg", "frankfurt"]),
    ("NL", ["netherlands", "amsterdam", "eindhoven", "rotterdam"]),
    ("CH", ["switzerland", "zurich", "zürich", "geneva", "lausanne"]),
    ("IE", ["ireland", "dublin"]),
    ("SG", ["singapore"]),
    ("HK", ["hong kong"]),
    ("AE", ["united arab emirates", "dubai", "abu dhabi"]),
    ("QA", ["qatar", "doha"]),
    ("SA", ["saudi", "riyadh", "jeddah", "kaust", "thuwal"]),
    ("CA", ["canada", "toronto", "vancouver", "montreal", "waterloo", "ottawa"]),
    ("JP", ["japan", "tokyo"]),
    ("IN", ["india", "bangalore", "bengaluru", "hyderabad"]),
]


def guess_country(location: str) -> str:
    loc = (location or "").lower()
    for code, needles in COUNTRY_HINTS:
        if any(n in loc for n in needles):
            return code
    return ""


def fingerprint(company: str, title: str, location: str) -> str:
    """The weakest identity: what the posting says about itself.

    Kept for postings that carry nothing better. On its own it is a guess, not
    an identity -- see identity_fingerprint.
    """
    raw = "|".join(re.sub(r"\s+", " ", (x or "").strip().lower()) for x in (company, title, location))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def identity_fingerprint(company: str, title: str, location: str, *,
                         ats: str = "", ats_job_id: str = "", url: str = "") -> str:
    """What makes this posting this posting, strongest signal available.

    The jobs table declares `fingerprint ... UNIQUE`, so whatever goes in here
    is the database's definition of "the same posting". Hashing company, title
    and location alone made that definition wrong in both directions:

      * renaming a company forked one posting into two rows, and an
        application dedupes by job_id, so the second row is a second
        application to the same employer;
      * and a firm advertising one role in two cities collided into a single
        row, so the second opening was never seen at all.

    An employer's own id for a posting settles both. A canonical URL settles
    it when there is no ATS id. Company+title+location is the last resort, and
    only for postings that carry neither -- which is scraped pages, where the
    URL is often session-specific.
    """
    ats = (ats or "").strip().lower()
    ats_job_id = str(ats_job_id or "").strip()
    if ats and ats != "unknown" and ats_job_id:
        raw = f"ats:{ats}:{ats_job_id}"
    else:
        canon = canonical_url(url)
        raw = f"url:{canon}" if canon else None
        if raw is None:
            return fingerprint(company, title, location)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def canonical_url(url: str) -> str:
    """The stable part of a posting URL: no tracking, no trailing slash.

    The same posting arrives from several sources wearing different query
    strings -- `?gh_src=Simplify`, `&utm_source=...` -- and each would
    otherwise look like a different job.
    """
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    if not host:
        return (url or "").strip().lower()
    port = f":{p.port}" if p.port and p.port not in (80, 443) else ""
    return f"{p.scheme.lower() or 'https'}://{host}{port}{path}"


def domain_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class RawJob:
    company: str
    title: str
    location: str
    url: str
    source: str
    ats: str = "unknown"
    ats_job_id: str = ""
    description: str = ""
    season: str = ""
    role_kind: str = "internship"
    posted_at: str = ""
    remote: bool = False

    def to_row(self) -> dict[str, Any]:
        return {
            "fingerprint": identity_fingerprint(
                self.company, self.title, self.location,
                ats=self.ats, ats_job_id=self.ats_job_id, url=self.url),
            "weak_key": fingerprint(self.company, self.title, self.location),
            "source": self.source, "ats": self.ats, "ats_job_id": self.ats_job_id,
            "company": self.company, "company_domain": domain_from_url(self.url),
            "title": self.title, "location": self.location,
            "country": guess_country(self.location), "remote": int(self.remote),
            "url": self.url, "apply_url": self.url, "description": self.description,
            "season": self.season, "role_kind": self.role_kind, "posted_at": self.posted_at,
        }


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": UA}, timeout=TIMEOUT, follow_redirects=True)


# --------------------------------------------------------------------------- #
# aggregate feeds
# --------------------------------------------------------------------------- #
def from_simplify(url: str, *, limit: int | None = None) -> Iterator[RawJob]:
    """SimplifyJobs listings.json. ~16k records, refreshed ~:00 and :30."""
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        try:
            data = r.json()
        except json.JSONDecodeError:
            return
    n = 0
    for item in data if isinstance(data, list) else data.get("listings", []):
        if not item.get("active", True):
            continue
        locs = item.get("locations") or []
        loc = ", ".join(locs) if isinstance(locs, list) else str(locs)
        link = item.get("url") or ""
        if not link:
            continue
        yield RawJob(
            company=item.get("company_name") or item.get("company") or "",
            title=item.get("title") or "",
            location=loc, url=link, source="simplify",
            ats=_ats_from_url(link), season=item.get("season") or "",
            posted_at=str(item.get("date_posted") or ""),
            remote="remote" in loc.lower(),
        )
        n += 1
        if limit and n >= limit:
            return


def from_csv_tracker(url: str, *, limit: int | None = None) -> Iterator[RawJob]:
    with _client() as c:
        r = c.get(url)
        r.raise_for_status()
        text = r.text
    n = 0
    for row in csv.DictReader(io.StringIO(text)):
        link = row.get("url") or ""
        if not link:
            continue
        loc = row.get("location") or ""
        yield RawJob(
            company=row.get("company") or "", title=row.get("title") or "",
            location=loc, url=link, source="zshah", ats=_ats_from_url(link),
            season=row.get("season") or "", posted_at=row.get("posted_at") or "",
            remote="remote" in loc.lower(),
        )
        n += 1
        if limit and n >= limit:
            return


def _ats_from_url(url: str) -> str:
    for name, pat in BOARD_PATTERNS.items():
        if pat.search(url):
            return name
    if "myworkdayjobs.com" in url:
        return "workday"
    return "unknown"


def mine_board_tokens(jobs: list[RawJob]) -> dict[str, set[str]]:
    """Harvest ATS board slugs from apply URLs — free company discovery."""
    out: dict[str, set[str]] = {k: set() for k in BOARD_PATTERNS}
    for j in jobs:
        for name, pat in BOARD_PATTERNS.items():
            m = pat.search(j.url)
            if m:
                out[name].add(m.group(1))
    return out


# --------------------------------------------------------------------------- #
# direct ATS boards -- full descriptions, freshest data
# --------------------------------------------------------------------------- #
# A board token is not a company name. Greenhouse hands back
# "alphasightsresumedrop" for AlphaSights' campus board, and that string then
# becomes the company in the tracker, in the artifact directory name, and in
# anything the cover letter derives from the company field. The board endpoint
# knows the real name, costs one request, and is cached for the process.
_BOARD_NAME_NOISE = re.compile(
    r"\s*(?:(?:campus|university|college|student|global|early|new\s*grad)[\s-]*)*"
    r"(?:recruiting|recruitment|careers?|jobs?|hiring|talent|"
    r"resume\s*drop|job\s*board|openings)\s*$", re.I)


@lru_cache(maxsize=128)
def greenhouse_board_name(token: str) -> str:
    """The employer's own name for a Greenhouse board, or "" if unavailable."""
    try:
        with _client() as c:
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        if r.status_code != 200:
            return ""
        name = str((r.json() or {}).get("name") or "").strip()
    except (httpx.HTTPError, ValueError):
        return ""
    # "AlphaSights Campus Recruiting" sheds one suffix per pass.
    prev = None
    while name and name != prev:
        prev = name
        name = _BOARD_NAME_NOISE.sub("", name).strip(" -\u2013\u2014|,")
    return name


def greenhouse_board(token: str) -> Iterator[RawJob]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    with _client() as c:
        r = c.get(url)
        if r.status_code != 200:
            return
        data = r.json()
    company = greenhouse_board_name(token) or token
    for j in data.get("jobs", []):
        yield RawJob(
            company=company, title=j.get("title", ""),
            location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""), source="greenhouse", ats="greenhouse",
            ats_job_id=str(j.get("id", "")),
            description=_strip_html(j.get("content", "")),
            posted_at=str(j.get("updated_at", "")),
        )


def lever_board(token: str) -> Iterator[RawJob]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    with _client() as c:
        r = c.get(url)
        if r.status_code != 200:
            return
        data = r.json()
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        yield RawJob(
            company=token, title=j.get("text", ""), location=cats.get("location", ""),
            url=j.get("hostedUrl", ""), source="lever", ats="lever",
            ats_job_id=str(j.get("id", "")),
            description=_strip_html(j.get("descriptionPlain") or j.get("description", "")),
            posted_at=str(j.get("createdAt", "")),
        )


def ashby_board(token: str) -> Iterator[RawJob]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
    with _client() as c:
        r = c.get(url)
        if r.status_code != 200:
            return
        data = r.json()
    for j in data.get("jobs", []):
        yield RawJob(
            company=token, title=j.get("title", ""), location=j.get("location", ""),
            url=j.get("jobUrl", ""), source="ashby", ats="ashby",
            ats_job_id=str(j.get("id", "")),
            description=_strip_html(j.get("descriptionHtml") or j.get("descriptionPlain", "")),
            posted_at=str(j.get("publishedAt", "")),
            remote=bool(j.get("isRemote")),
        )


BOARD_FETCHERS = {
    "greenhouse": greenhouse_board,
    "lever": lever_board,
    "ashby": ashby_board,
}


# --------------------------------------------------------------------------- #
# Subdomains a company puts its careers site on; the company is the parent.
_CAREERS_SUBS = ("jobs", "careers", "career", "apply", "work", "join",
                 "hiring", "talent", "recruiting", "www", "boards", "job")

# Separators a page title uses between the role and the employer.
_TITLE_SPLIT = re.compile(r"\s+[|\u2013\u2014\u00b7-]\s+|\s+at\s+", re.I)


def company_from_page(html: str, url: str) -> str:
    """The employer's name, as the page gives it.

    `domain_from_url(url).split(".")[0].title()` was the whole of this, which
    turns careers.acme.com into "Careers" and a loopback test server into
    "127". The name ends up on the tracker, in the artifact directory, and in
    whatever the cover letter says about where it is applying.
    """
    for pattern in (r'property=["\']og:site_name["\'][^>]*content=["\']([^"\']{1,80})',
                    r'content=["\']([^"\']{1,80})["\'][^>]*property=["\']og:site_name["\']',
                    r'name=["\']application-name["\'][^>]*content=["\']([^"\']{1,80})'):
        m = re.search(pattern, html or "", re.I)
        if m and m.group(1).strip():
            return m.group(1).strip()[:80]

    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    if m:
        parts = [p.strip() for p in _TITLE_SPLIT.split(re.sub(r"\s+", " ", m.group(1)))]
        parts = [p for p in parts if p]
        # "Software Engineer - Acme" puts the employer last; a single-part
        # title is the role, not the company, so it is not used.
        if len(parts) >= 2 and 1 < len(parts[-1]) <= 60:
            return parts[-1]

    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    labels = [p for p in host.split(".") if p]
    while len(labels) > 2 and labels[0] in _CAREERS_SUBS:
        labels = labels[1:]
    stem = labels[0] if labels else ""
    return stem.replace("-", " ").title() if stem else "Unknown"


def fetch_single(url: str) -> RawJob | None:
    """Fetch one posting from a link. Used by `applier apply <url>`.

    Tries the ATS API first (structured, complete) and falls back to scraping
    the page text.
    """
    ats = _ats_from_url(url)

    if ats == "greenhouse":
        m = BOARD_PATTERNS["greenhouse"].search(url)
        jid = re.search(r"/jobs/(\d+)", url)
        if m and jid:
            for j in greenhouse_board(m.group(1)):
                if j.ats_job_id == jid.group(1):
                    return j
    if ats == "lever":
        m = BOARD_PATTERNS["lever"].search(url)
        if m:
            tail = url.rstrip("/").split("/")[-1]
            for j in lever_board(m.group(1)):
                if j.ats_job_id == tail:
                    return j
    if ats == "ashby":
        m = BOARD_PATTERNS["ashby"].search(url)
        if m:
            for j in ashby_board(m.group(1)):
                if j.url.rstrip("/") == url.rstrip("/"):
                    return j

    # generic fallback: fetch and strip
    try:
        with _client() as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError:
        return None

    title = ""
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if mt:
        title = re.sub(r"\s+", " ", mt.group(1)).strip()
    return RawJob(
        company=company_from_page(html, url),
        title=title[:180], location="", url=url, source="manual",
        ats=ats, description=_strip_html(html)[:20000],
    )


_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    if not html:
        return ""
    import html as _h
    text = _TAG_RE.sub(" ", html)
    text = _ANY_TAG.sub(" ", text)
    return re.sub(r"\s+", " ", _h.unescape(text)).strip()
