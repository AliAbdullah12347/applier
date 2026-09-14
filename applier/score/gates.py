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


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


# --------------------------------------------------------------------------- #
def check_gates(job: dict[str, Any], settings: Config, profile: Config,
                db=None) -> GateResult:
    """Hard eligibility. Cheap string work only — no network, no model."""
    g = settings.get("gates", {})
    blob = _norm(f"{job.get('title','')} {job.get('description','')}")

    # 1. citizenship / clearance / export control -> categorically impossible
    for phrase in g.get("hard_reject_phrases", []):
        if _norm(phrase) in blob:
            return GateResult(False, f"requires: {phrase}", "refuses")

    # 2. programmes restricted to first/second years
    title = _norm(job.get("title", ""))
    for prog in g.get("hard_reject_programs", []):
        if _norm(prog) in title:
            return GateResult(False, f"{prog} is first/second-year only", "silent")

    # 3. graduation window, when the posting states one
    grad = str(g.get("graduation_date", ""))
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

    return GateResult(True, "", sponsorship)


def check_caps(job: dict[str, Any], settings: Config, db) -> GateResult | None:
    """Application caps are the binding constraint, not deadlines.

    Google allows 3 per 30 days; Optiver imposes an 8-month GLOBAL lockout that
    triggers on *starting* an assessment, not failing it. Burning one of these
    slots badly is unrecoverable within a cycle.
    """
    domain = (job.get("company_domain") or "").lower()
    if not domain:
        return None
    caps = settings.get("gates.application_caps", {}) or {}
    rule = caps.get(domain)
    if not rule:
        return None

    row = db.one("SELECT * FROM company_caps WHERE domain=?", (domain,))
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
    parts["sponsorship"] = {"sponsors": 0.20, "silent": 0.10, "refuses": 0.0}[gate.sponsorship]
    if gate.sponsorship == "sponsors":
        notes.append("states sponsorship")

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
