"""Eligibility gates and match scoring.

Gates run BEFORE any tailoring work. This ordering is the single biggest time
saver in the system: there is no point spending an LLM call and a LaTeX compile
on a posting that requires US citizenship.

A gate failure zeroes the score. It is never a weighted term — a role you cannot
legally hold is not a 0.4 match, it is a non-match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Config

# --------------------------------------------------------------------------- #


@dataclass
class GateResult:
    passed: bool
    reason: str = ""
    sponsorship: str = "silent"      # sponsors | silent | refuses
    detail: dict = field(default_factory=dict)


@dataclass
class Score:
    value: float
    why: str
    breakdown: dict = field(default_factory=dict)


# "We do not sponsor visas **but will consider applicants with STEM/OPT
# eligibility**" is one sentence saying two opposite things, and for this
# candidate only the second half is operative. Reading the first half alone
# throws away a posting that explicitly invites him to apply.
#
# For an internship the question is moot in any case: CPT is the student's own
# work authorisation, granted by his institution, and is not something the
# employer sponsors at all. The clause only starts to bite at conversion, and
# a STEM degree carries three years of OPT before it does.
#
# Clearance, citizenship and export control are never qualified like this, so
# the carve-out deliberately only suppresses *sponsorship* rejections.
OPT_CARVE_OUT = re.compile(
    r"\b(?:but|however|though|although|unless|except|while|whilst)\b[^.;]{0,160}?"
    r"\b(?:stem[\s/-]*opt|opt|cpt|practical training|f-?1)\b", re.I)

# The other shape, and the commoner one. OPT_CARVE_OUT reads a refusal that is
# immediately qualified, which needs a "but" to hang on. A posting that just
# says "we welcome international students and support CPT and OPT" has no
# refusal to qualify, so that pattern never fires and the posting reads as
# silent -- which scores it below one that says nothing at all about
# sponsorship but happens to use one of the configured phrases verbatim.
#
# Negation is checked explicitly rather than hoped for: "we do not accept OPT"
# contains "accept" and "OPT" within sixty characters of each other.
OPT_WELCOME = re.compile(
    r"\b(support|accept|welcome|consider|sponsor|open to|eligible for|"
    r"encourage)\w*\b([^.;]{0,70}?)"
    r"\b(cpt|opt|practical training|f-?1 students?|international students?)\b",
    re.I)

_NEGATION = re.compile(r"\b(not|never|cannot|can't|unable|un(?:fortunately)?|"
                       r"no longer|do not|does not|will not|won't)\b", re.I)


# A welcome can sit on either side of the thing being welcomed:
# "we support CPT and OPT" and "F-1 visa holders are encouraged to apply" say the
# same thing in opposite orders, and matching only one of them missed half.
OPT_WELCOME_AFTER = re.compile(
    r"\b(cpt|opt|practical training|f-?1 students?|international students?)\b"
    r"([^.;]{0,70}?)\b(welcome|encouraged?|eligible|accepted|considered|"
    r"supported|invited)\w*\b",
    re.I)


def welcomes_opt(text: str) -> bool:
    """True when a posting positively says it takes CPT/OPT or F-1 visa holders.

    Negation is checked against the words between the two halves and a short
    run before them, because "we do not accept OPT" puts every token this
    looks for within sixty characters of each other.
    """
    text = text or ""
    for pattern, lead_group in ((OPT_WELCOME, 2), (OPT_WELCOME_AFTER, 2)):
        for m in pattern.finditer(text):
            window = text[max(0, m.start() - 36):m.start()] + " " + (m.group(lead_group) or "")
            if _NEGATION.search(window):
                continue
            return True
    return False

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


# --------------------------------------------------------------------------- #
def check_gates(job: dict[str, Any], settings: Config, profile: Config,
                db=None) -> GateResult:
    """Hard eligibility. Cheap string work only — no network, no model."""
    g = settings.get("gates", {})
    blob = _norm(f"{job.get('title','')} {job.get('description','')}")

    # A refusal to sponsor that is immediately qualified is not a refusal.
    carve_out = bool(OPT_CARVE_OUT.search(job.get("description", "") or ""))

    # 1. citizenship / clearance / export control -> categorically impossible
    for phrase in g.get("hard_reject_phrases", []):
        if _norm(phrase) not in blob:
            continue
        if "sponsor" in phrase.lower() and carve_out:
            continue
        return GateResult(False, f"requires: {phrase}", "refuses")

    # 2. programmes restricted to first/second years
    title = _norm(job.get("title", ""))
    for prog in g.get("hard_reject_programs", []):
        if _norm(prog) in title:
            return GateResult(False, f"{prog} is first/second-year only", "silent")

    # 3. graduation window, when the posting states one.
    #
    # Read from the profile, which is the one place a graduation date belongs.
    # It used to be duplicated into settings.yaml as `gates.graduation_date`,
    # which was both a second source of truth that could disagree with the
    # first, and a real personal detail sitting in the file this project
    # publishes. The settings key is still honoured as a fallback so an
    # existing configuration keeps working, but the profile wins.
    edu = (profile.get("education", []) or [{}])[0]
    grad = str(edu.get("expected_graduation") or g.get("graduation_date", "") or "")
    if grad:
        window = _graduation_window(blob)
        if window and not _grad_in_window(grad, window):
            return GateResult(False, f"graduation window {window[0]}..{window[1]} excludes {grad}")

    # 4. title exclusions (senior/staff/manager...)
    for bad in settings.get("search.titles_exclude", []):
        if _norm(bad) in title:
            return GateResult(False, f"title excluded: {bad.strip()}")

    # 5. per-employer caps and lockouts
    if db is not None:
        capres = check_caps(job, settings, db)
        if capres is not None:
            return capres

    # positive sponsorship signal (ranking only, never a filter)
    sponsorship = "silent"
    for phrase in g.get("sponsor_friendly_phrases", []):
        if _norm(phrase) in blob:
            sponsorship = "sponsors"
            break
    else:
        if carve_out or welcomes_opt(job.get("description", "") or ""):
            # Not sponsorship, but an employer that has thought about OPT and
            # said yes is a materially better prospect than one that is silent.
            sponsorship = "opt_ok"

    return GateResult(True, "", sponsorship)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# Suffixes that are part of a company's legal name rather than its identity,
# stripped before matching so "Optiver US LLC" still matches "optiver.com".
_LEGAL_SUFFIXES = ("inc", "llc", "ltd", "limited", "corp", "corporation",
                   "plc", "gmbh", "bv", "sa", "ag", "co", "group", "holdings",
                   "technologies", "technology", "labs", "us", "usa")


def cap_key_for(job: dict[str, Any], caps: dict) -> str | None:
    """Which configured cap, if any, governs this posting.

    Caps are keyed by employer domain -- "optiver.com", "google.com" -- but a
    posting hosted on an applicant tracking system carries the ATS's domain,
    not the employer's. Every Greenhouse posting in the database has
    company_domain "job-boards.greenhouse.io", 733 of them at the time this
    was written, so matching on the domain alone meant *none* of these caps
    had ever fired for an ATS-hosted job.

    That is the expensive direction to get wrong. Optiver's rule is an
    eight-month global lockout triggered by starting the assessment, and Jump
    allows one region ever. A cap that silently does not apply is worse than
    no cap, because it is relied upon.

    So the employer's own name is matched as well. The name is compared on its
    stem, with legal suffixes removed, and a stem shorter than four characters
    is required to match exactly -- otherwise a cap on a short domain would
    swallow every company containing those letters.
    """
    domain = (job.get("company_domain") or "").lower()
    name = _slug(job.get("company", ""))
    for word in _LEGAL_SUFFIXES:
        if name.endswith(word) and len(name) > len(word) + 2:
            name = name[: -len(word)]
            break

    for key, _rule in caps.items():
        k = str(key).lower()
        if domain and k == domain:
            return key
        stem = _slug(k.split(".")[0])
        if not stem or not name:
            continue
        if name == stem:
            return key
        if len(stem) >= 4 and (name.startswith(stem) or stem in name):
            return key
    return None


def check_caps(job: dict[str, Any], settings: Config, db) -> GateResult | None:
    """Application caps are the binding constraint, not deadlines.

    Google allows 3 per 30 days; Optiver imposes an 8-month GLOBAL lockout that
    triggers on *starting* an assessment, not failing it. Burning one of these
    slots badly is unrecoverable within a cycle.
    """
    caps = settings.get("gates.application_caps", {}) or {}
    if not caps:
        return None
    key = cap_key_for(job, caps)
    if not key:
        return None
    rule = caps[key]

    # Counted against the cap's own key, so one employer's quota is one row
    # however its postings happen to be hosted.
    row = db.one("SELECT * FROM company_caps WHERE domain=?", (str(key).lower(),))
    if row:
        if row["locked_until"]:
            try:
                if datetime.fromisoformat(row["locked_until"]) > datetime.now():
                    return GateResult(False, f"locked until {row['locked_until']} ({rule.get('note','')})")
            except ValueError:
                pass
        window = timedelta(days=int(rule.get("window_days", 30)))
        if row["last_applied"]:
            try:
                last = datetime.fromisoformat(row["last_applied"])
                if datetime.now() - last < window and row["applied_count"] >= int(rule.get("max", 1)):
                    return GateResult(
                        False,
                        f"cap reached: {row['applied_count']}/{rule['max']} in "
                        f"{rule.get('window_days')}d. {rule.get('note','')}".strip(),
                    )
            except ValueError:
                pass
    return None


# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}

_WINDOW_RE = re.compile(
    r"(?:graduat\w*|degree|completion)[^.]{0,80}?"
    r"(?:between\s+)?([a-z]+)\s+(20\d{2})\s*(?:and|to|-|–|through)\s*([a-z]+)\s+(20\d{2})",
    re.I,
)


def _graduation_window(blob: str) -> tuple[str, str] | None:
    m = _WINDOW_RE.search(blob)
    if not m:
        return None
    m1, y1, m2, y2 = m.groups()
    i1, i2 = _MONTHS.get(m1.lower()), _MONTHS.get(m2.lower())
    if not i1 or not i2:
        return None
    return (f"{y1}-{i1:02d}", f"{y2}-{i2:02d}")


def _grad_in_window(grad: str, window: tuple[str, str]) -> bool:
    return window[0] <= grad <= window[1]


# --------------------------------------------------------------------------- #
def score_job(job: dict[str, Any], settings: Config, profile: Config,
              gate: GateResult) -> Score:
    """Weighted relevance, 0..1. Only called for postings that passed the gates."""
    if not gate.passed:
        return Score(0.0, gate.reason)

    blob = _norm(f"{job.get('title','')} {job.get('description','')}")
    title = _norm(job.get("title", ""))
    parts: dict[str, float] = {}
    notes: list[str] = []

    # --- title relevance (0.30) ---
    includes = [_norm(t) for t in settings.get("search.titles_include", [])]
    hits = sum(1 for t in includes if t and t in title)
    parts["title"] = min(1.0, hits / 2.0) * 0.30
    if hits:
        notes.append("title match")

    # --- skill overlap (0.30) ---
    skills = _profile_skills(profile)
    matched = [s for s in skills if s and s in blob]
    parts["skills"] = (len(matched) / max(6, len(skills) or 1)) * 0.30
    if matched:
        notes.append(f"{len(matched)} skills")

    # --- location preference (0.20) ---
    country = (job.get("country") or "").upper()
    weight = 0.5
    for loc in settings.get("search.locations_preferred", []):
        if loc.get("country", "").upper() == country:
            weight = float(loc.get("weight", 0.5)); break
    if job.get("remote"):
        weight = max(weight, 0.95)
    parts["location"] = weight * 0.20

    # --- sponsorship signal (0.20) --- the field that matters most here
    parts["sponsorship"] = {"sponsors": 0.20, "opt_ok": 0.17,
                            "silent": 0.10, "refuses": 0.0}.get(gate.sponsorship, 0.10)
    if gate.sponsorship == "sponsors":
        notes.append("states sponsorship")
    elif gate.sponsorship == "opt_ok":
        notes.append("accepts OPT/CPT")

    total = round(sum(parts.values()), 4)
    return Score(total, ", ".join(notes) or "weak match", parts)


def _profile_skills(profile: Config) -> list[str]:
    """Skills the candidate can actually defend, drawn from the atom bank tags
    when present and the profile otherwise."""
    out: set[str] = set()
    for edu in profile.get("education", []) or []:
        for c in edu.get("coursework", []) or []:
            out.add(_norm(c))
    for extra in ("python", "c++", "typescript", "react", "pytorch", "sql",
                  "linux", "git", "javascript", "java", "next.js", "docker"):
        out.add(extra)
    return sorted(out)
