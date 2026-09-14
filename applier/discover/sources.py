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
    raw = "|".join(re.sub(r"\s+", " ", (x or "").strip().lower()) for x in (company, title, location))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


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
            "fingerprint": fingerprint(self.company, self.title, self.location),
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
def greenhouse_board(token: str) -> Iterator[RawJob]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    with _client() as c:
        r = c.get(url)
        if r.status_code != 200:
            return
        data = r.json()
    for j in data.get("jobs", []):
        yield RawJob(
            company=token, title=j.get("title", ""),
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
        company=domain_from_url(url).split(".")[0].title(),
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
