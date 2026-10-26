"""Reading people off a page the company published.

The first version of this flattened the HTML to text and looked for two
capitalised words with a job-title word somewhere in the surrounding ninety
characters. On a real team page that matches the navigation bar. Amplify's
leadership page produced "Blog Trust", "Careers News", "Center Contact" and
"Company About" -- adjacent menu items, read as people, each about to be given
a synthesised email address and a message in Ali's name.

Two changes fix it, and both are about structure rather than cleverness:

* **Blocks, not a stream.** "Blog" and "Trust" are separate links. Once text
  is grouped by the element that contains it, they can never form a name.
* **Structured data first.** Many sites publish schema.org `Person` entries in
  a JSON-LD script tag, which is the company stating who these people are
  rather than us inferring it.

Stdlib only: the packaged application claims to be self-contained, and a
parser dependency that must be present at runtime would make that untrue.
"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from typing import Iterator

# Elements that end a run of text. A name and a job title inside the same card
# belong together; a name in the nav and a title in the footer do not.
BLOCK_TAGS = {
    "div", "p", "li", "ul", "ol", "section", "article", "aside", "header",
    "footer", "nav", "main", "td", "th", "tr", "table", "figure", "figcaption",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "a", "button", "label",
    "blockquote", "dd", "dt", "dl", "form", "option", "picture", "img",
}
SKIP_CONTENT = {"script", "style", "noscript", "svg", "template", "head"}

ROLE_RE = re.compile(
    r"\b(chief\s+\w+\s+officer|c[eotifmrsp]o\b|vp\b|vice\s+president|head\s+of\s+[a-z ]{3,24}|"
    r"senior|staff|principal|lead\b|director|manager|founder|co-?founder|partner|"
    r"engineer|engineering|developer|scientist|researcher|research|recruiter|"
    r"recruiting|talent|analyst|architect|designer|counsel|controller|president|"
    r"chair|trustee|advisor|associate|specialist|coordinator|consultant)\b",
    re.I)

NAME_TOKEN = r"[A-Z][a-z'’à-ÿ-]{1,19}"
NAME_RE = re.compile(rf"\b({NAME_TOKEN})(?:\s+({NAME_TOKEN}|[A-Z]\.))?\s+({NAME_TOKEN})\b|"
                     rf"\b({NAME_TOKEN})\s+({NAME_TOKEN})\b")

# Capitalised words that appear on every website and are never a person.
NOT_A_NAME = {
    # navigation and marketing
    "about", "contact", "careers", "career", "blog", "news", "home", "team",
    "our", "the", "we", "us", "your", "my", "privacy", "policy", "terms",
    "service", "services", "support", "sales", "press", "learn", "more",
    "read", "sign", "log", "in", "out", "get", "started", "company", "center",
    "centre", "trust", "help", "docs", "documentation", "product", "products",
    "solutions", "pricing", "resources", "events", "partners", "customers",
    "community", "platform", "overview", "features", "security", "legal",
    "cookie", "cookies", "settings", "search", "menu", "close", "open", "next",
    "previous", "all", "view", "download", "watch", "join", "apply", "jobs",
    "job", "work", "working", "life", "culture", "values", "mission", "vision",
    "story", "history", "leadership", "board", "directors", "trustees",
    "investors", "media", "email", "phone", "address", "newsletter", "follow",
    "share", "skip", "main", "content", "toggle", "navigation", "copyright",
    "rights", "reserved", "inc", "llc", "ltd", "corp", "group", "global",
    "worldwide", "international", "foundation", "institute", "university",
    "college", "school", "hospital", "bank", "capital", "ventures", "labs",
    "studio", "agency", "login", "logout", "register", "subscribe", "accept",
    "manage", "preferences", "statement", "notice", "disclosure", "report",
    "annual", "quarterly", "investor", "relations", "ethics", "conduct",
    "diversity", "inclusion", "sustainability", "impact", "giving", "donate",
    "volunteer", "membership", "benefits", "perks", "office", "offices",
    "locations", "location", "remote", "hybrid", "onsite", "full", "part",
    "time", "intern", "internship", "graduate", "entry", "level", "apply",
    # months and days
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    # the commonest place words that pair up into a false name
    "united", "states", "kingdom", "new", "york", "san", "francisco", "los",
    "angeles", "america", "north", "south", "east", "west", "city",
    "county", "state", "street", "avenue", "road", "suite", "floor",
    # Organisational vocabulary. Every one of these was extracted as part of
    # a person's name from a real page: "Chief Executive Officer", "Deputy
    # Chief", "Wikimedia Enterprise", "Fundraising Operations", "Internal
    # Communications". A job title is not a name, however capitalised.
    "chief", "executive", "officer", "deputy", "senior", "junior", "staff",
    "principal", "president", "director", "manager", "head", "lead", "leader",
    "vice", "associate", "assistant", "specialist", "coordinator", "engineer",
    "engineering", "scientist", "researcher", "research", "analyst",
    "architect", "designer", "counsel", "controller", "chair", "chairman",
    "chairwoman", "trustee", "advisor", "adviser", "consultant", "enterprise",
    "operations", "fundraising", "communications", "communication", "internal",
    "external", "regional", "technical", "technology", "product", "program",
    "programme", "project", "business", "marketing", "finance", "financial",
    "human", "people", "talent", "data", "software", "hardware", "platform",
    "infrastructure", "development", "developer", "design", "strategy",
    "strategic", "growth", "revenue", "customer", "client", "account",
    "partnerships", "recruiting", "recruiter", "recruitment", "administration",
    "administrative", "executive", "general", "managing", "founding", "founder",
    "cofounder", "owner", "principal", "fellow", "scholar", "professor",
    "doctor", "literacy", "learning", "education", "curriculum", "academic",
}

MAX_BLOCK_CHARS = 260          # a person card is short; an article is not


class _Blocks(HTMLParser):
    """Collect text, broken at every block-level boundary."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip = 0
        self.jsonld: list[str] = []
        self._in_ld = False

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text:
            self.blocks.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_CONTENT:
            self._skip += 1
            if tag == "script":
                kind = dict(attrs).get("type", "")
                self._in_ld = "ld+json" in (kind or "").lower()
            return
        if tag in BLOCK_TAGS:
            self._flush()
        # An image's alt text is often the only place a person's name appears.
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt.strip():
                self.blocks.append(re.sub(r"\s+", " ", alt).strip())

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT:
            self._skip = max(0, self._skip - 1)
            self._in_ld = False
            return
        if tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._in_ld:
            self.jsonld.append(data)
            return
        if self._skip:
            return
        self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def _plausible_name(candidate: str) -> str:
    """Return a cleaned personal name, or "" if this is not one."""
    candidate = unescape(candidate or "").strip(" ,·|-–—•")
    if not candidate or any(ch.isdigit() for ch in candidate):
        return ""
    parts = [p for p in re.split(r"\s+", candidate) if p]
    if not 2 <= len(parts) <= 4:
        return ""
    cleaned: list[str] = []
    for p in parts:
        bare = re.sub(r"[^A-Za-z'’-]", "", p)
        if not bare:
            return ""
        if bare.lower() in NOT_A_NAME:
            return ""
        if bare.isupper() and len(bare) > 2:
            return ""                      # SHOUTED nav items
        if not bare[0].isupper():
            return ""
        cleaned.append(p)
    # A middle initial is fine; everything else must be a word.
    if any(len(re.sub(r"[^A-Za-z]", "", c)) < 2 and not c.endswith(".")
           for c in cleaned):
        return ""
    # A job title is not a name. "Research Hub" and "Early Literacy" are
    # section headings on a leadership page, and they reached this point
    # because the block they sit in mentions research.
    if any(ROLE_RE.fullmatch(re.sub(r"[^A-Za-z-]", "", c)) for c in cleaned):
        return ""
    return " ".join(cleaned)


def _from_jsonld(chunks: list[str]) -> Iterator[tuple[str, str]]:
    """(name, role) pairs the site published as schema.org Person entries."""
    def walk(node):
        if isinstance(node, dict):
            types = node.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any(str(t).lower() == "person" for t in types):
                name = str(node.get("name") or "").strip()
                role = node.get("jobTitle") or node.get("description") or ""
                if isinstance(role, (list, tuple)):
                    role = role[0] if role else ""
                if name:
                    yield name, str(role).strip()
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    for chunk in chunks:
        try:
            data = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        yield from walk(data)


def people_on_page(html: str, *, limit: int = 25,
                   heuristic: bool = False) -> list[tuple[str, str]]:
    """(name, role) for everyone this page presents as a person.

    `heuristic` is off by default, and that default is the whole lesson of
    this module. Reading names out of arbitrary markup with a regex does not
    work: it found "Blog Trust" and "Careers News" from a navigation bar, and
    then, after two rounds of tightening, "Chief Executive Officer" and
    "Fundraising Operations" from a staff directory. Each round removed one
    class of nonsense and revealed another.

    That failure is not acceptable here, because the output of this function
    becomes a person with a synthesised email address who receives a message
    signed with Ali's real name. Inventing a recipient is worse than finding
    nobody. So the default is structured data only -- schema.org Person
    entries, which are the company stating who these people are -- and the
    heuristic path stays available for a human who is reading the results.
    """
    parser = _Blocks()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        pass

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(name: str, role: str) -> None:
        clean = _plausible_name(name)
        if not clean:
            return
        key = re.sub(r"[^a-z]", "", clean.lower())
        if key in seen:
            return
        seen.add(key)
        out.append((clean, re.sub(r"\s+", " ", role or "").strip()[:80]))

    for name, role in _from_jsonld(parser.jsonld):
        add(name, role)
        if len(out) >= limit:
            return out

    if not heuristic:
        return out

    for block in parser.blocks:
        if len(block) > MAX_BLOCK_CHARS:
            continue
        role_match = ROLE_RE.search(block)
        if not role_match:
            continue
        # The name is whatever precedes the role in this block, or the block
        # itself when the role came from a neighbouring line of the same card.
        head = block[: role_match.start()].strip(" ,·|-–—•\n") or block
        name_match = NAME_RE.search(head)
        if not name_match:
            continue
        candidate = name_match.group(0)
        add(candidate, role_match.group(0))
        if len(out) >= limit:
            break

    return out


def fetch_rendered(url: str, *, timeout_ms: int = 20_000) -> str:
    """The page as a browser builds it, or "" if that is not possible here.

    Team pages are routinely a React shell: the static HTML contains a loading
    div and the people arrive from an API afterwards. Amplify's leadership page
    and the Wikimedia Foundation's about page are both like this, and a plain
    GET of either yields no people at all.

    Playwright is already a dependency of this project for form filling, so
    this costs nothing new. It is still optional at runtime -- a machine with
    no browser installed gets "" and falls back to the static HTML, because
    thinner outreach is an acceptable outcome and a crash is not.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return ""

    # A real browser pointed at an attacker-chosen URL is a worse primitive
    # than a GET, so the same guard applies before one is opened.
    from ..net import is_public_url
    if not is_public_url(url):
        return ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"))
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                return page.content()
            finally:
                browser.close()
    except Exception:
        return ""
