"""The answer bank — how the system asks once and remembers forever.

Resolution order for any form field:

    1. LEGAL fields      -> profile.yaml, verbatim. Never fuzzy-matched, never
                            LLM-generated. Exact semantic match required, or halt.
    2. Stored answers    -> previously answered questions, matched exactly, then
                            fuzzily above a threshold.
    3. Profile mapping   -> direct facts (name, phone, school, dates).
    4. LLM               -> only for genuinely free-text/subjective questions.
    5. Ask queue         -> anything left. You clear it once; it never asks again.

Why legal fields are special
----------------------------
A wrong answer to a citizenship or work-authorisation question is not merely a bad
application. For any visa-dependent applicant the same statement is repeated later
on government forms, where an inconsistency can be treated as misrepresentation
with consequences that far outlast the job. So these fields are resolved by exact
classification against a fixed taxonomy and copied verbatim from the profile; if
classification is ambiguous the run halts and asks rather than guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from ..config import Config
from ..db import Database, now

# --------------------------------------------------------------------------- #
# question classification
# --------------------------------------------------------------------------- #

# Each entry: (canonical_id, [regex patterns], is_legal)
# Order matters -- the first match wins, so put the specific before the general.
QUESTION_TAXONOMY: list[tuple[str, list[str], bool]] = [
    # ---- LEGAL / immigration -------------------------------------------- #
    ("sponsorship_future", [
        r"(will|do|would) you .{0,40}(require|need) .{0,30}sponsor",
        r"(require|need) .{0,30}sponsorship",
        r"future.*sponsor",
        r"sponsorship (now or in the future|for employment)",
        r"immigration (support|sponsorship|assistance)",
    ], True),
    ("authorized_now", [
        r"are you (legally )?(authorized|authorised|eligible) to work",
        r"legally authorized to work",
        r"right to work",
        r"work authorization status",
    ], True),
    ("citizenship", [
        r"are you a (u\.?s\.?|united states) citizen",
        r"citizenship status",
        r"what is your citizenship",
        r"country of citizenship",
    ], True),
    ("visa_status", [r"visa status", r"current immigration status", r"what visa"], True),
    ("security_clearance", [r"security clearance", r"active clearance", r"clearance level"], True),
    ("export_control", [r"export control", r"itar"], True),
    ("legal_name", [r"^legal (first |last |full )?name", r"name as it appears"], True),
    ("date_of_birth", [r"date of birth", r"^dob$", r"birth ?date"], True),
    ("graduation_date", [
        r"(expected |anticipated )?graduation date",
        r"when do you graduate", r"expected completion", r"degree completion date",
    ], True),
    ("degree_type", [r"degree type", r"what degree", r"level of (degree|education)"], True),

    # ---- voluntary self-identification ---------------------------------- #
    ("gender", [r"\bgender\b", r"gender identity"], False),
    ("ethnicity", [r"ethnicit", r"\brace\b", r"racial", r"hispanic", r"latino"], False),
    ("veteran", [r"veteran", r"armed forces", r"military service"], False),
    ("disability", [r"disabilit(?:y|ies)", r"\bdisabled\b", r"chronic condition",
                    r"major life activit"], False),
    ("pronouns", [r"pronoun"], False),

    # ---- factual (from profile, safe to auto-fill) ---------------------- #
    ("first_name", [r"^first name", r"given name"], False),
    ("last_name", [r"^last name", r"family name", r"surname"], False),
    ("full_name", [r"^(full )?name$", r"your name"], False),
    ("email", [r"e-?mail"], False),
    ("phone", [r"phone", r"mobile", r"telephone", r"contact number"], False),
    ("address_line1", [r"address line ?1", r"street address", r"^address$"], False),
    ("city", [r"^city", r"town"], False),
    ("state", [r"^state", r"province", r"region"], False),
    ("postal_code", [r"zip", r"postal code", r"post ?code"], False),
    ("country", [r"^country"], False),
    ("linkedin", [r"linked ?in"], False),
    ("github", [r"git ?hub"], False),
    ("website", [r"portfolio", r"personal website", r"^website", r"other url"], False),
    ("school", [r"school", r"university", r"college", r"institution"], False),
    # "major" alone is dangerous: EEO disability questions ask about a "major
    # life activity", and a bare keyword match put the candidate's degree in a
    # disability field on a real Greenhouse form. Require education context.
    ("major", [r"\bmajor(?:s|ing)?\b(?!\s+life)", r"field of study", r"discipline",
               r"course of study", r"area of study", r"concentration"], False),
    ("gpa", [r"\bgpa\b", r"grade point"], False),
    ("start_date", [r"start date", r"available.*start", r"earliest.*start", r"when can you start"], False),
    ("end_date", [r"end date", r"available.*until", r"availability end"], False),
    ("salary", [r"salary", r"compensation", r"expected pay", r"hourly rate", r"desired pay"], False),
    ("relocate", [r"willing to relocate", r"open to relocation"], False),
    ("remote_pref", [r"remote", r"work location preference", r"hybrid"], False),
    ("how_heard", [r"how did you (hear|learn)", r"referral source", r"where did you find"], False),
    ("referred_by", [r"were you referred", r"referred by", r"employee referral"], False),
    ("previously_employed", [r"previously (worked|employed)", r"former employee", r"worked here before"], False),
    ("resume_upload", [r"resume", r"cv upload", r"attach.*resume"], False),
    ("cover_letter_upload", [r"cover letter"], False),

]

LEGAL_IDS = {qid for qid, _, legal in QUESTION_TAXONOMY if legal}

# Any question containing one of these is immigration- or identity-adjacent.
# If the taxonomy does not recognise it, the run stops and asks rather than
# guessing or handing it to a model. An unrecognised phrasing of "are you a
# citizen?" is exactly the case where falling through is most costly, so the
# default for this whole area is halt, not best-effort.
LEGAL_TRIPWIRES = (
    "citizen", "citizenship", "sponsor", "sponsorship", "visa", "immigration",
    "work authorization", "work authorisation", "authorized to work",
    "authorised to work", "right to work", "clearance", "itar", "export control",
    "green card", "permanent resident", "nationality", "passport",
    "date of birth", "legal name",
)


def normalize_question(text: str) -> str:
    """Stable key for a question, robust to punctuation and whitespace churn."""
    t = (text or "").lower().strip()
    t = re.sub(r"[*\u2217]+", "", t)                    # required-field asterisks
    t = re.sub(r"\(required\)|\(optional\)", "", t)
    t = re.sub(r"[^\w\s?]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Stripping punctuation turns "U.S." into "u s", which then matches no
    # pattern written as "u.s.". Collapse only these specific abbreviations --
    # a generic single-letter collapse would eat the article in
    # "are you a u s citizen" and produce "aus".
    t = re.sub(r"\bu s a\b", "usa", t)
    t = re.sub(r"\bu s\b", "us", t)
    t = re.sub(r"\bu k\b", "uk", t)
    return t


def classify(question: str) -> tuple[str | None, bool]:
    """Return (canonical_id, is_legal). (None, False) if unrecognised."""
    q = normalize_question(question)
    for qid, patterns, legal in QUESTION_TAXONOMY:
        for pat in patterns:
            if re.search(pat, q):
                return qid, legal
    return None, False


# --------------------------------------------------------------------------- #
@dataclass
class Resolution:
    value: Any
    source: str            # profile_legal | profile | stored | llm | default
    confidence: float
    question_id: str | None = None
    needs_qualifier: bool = False
    qualifier_text: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


class HaltForInput(Exception):
    """Raised when a legal field cannot be resolved unambiguously."""

    def __init__(self, question: str, reason: str):
        super().__init__(f"{reason}: {question!r}")
        self.question = question
        self.reason = reason


class AnswerBank:
    def __init__(self, db: Database, profile: Config, settings: Config) -> None:
        self.db = db
        self.p = profile
        self.s = settings
        self.threshold = float(settings.get("apply.universal_filler.confidence_threshold", 0.80))

    # ------------------------------------------------------------------ #
    def resolve(self, question: str, *, field_type: str = "text",
                options: list[str] | None = None, job: dict | None = None) -> Resolution:
        qid, is_legal = classify(question)

        if is_legal:
            return self._resolve_legal(question, qid, options)

        # Tripwire: legal-adjacent wording that the taxonomy missed. Halting
        # here costs one prompt; guessing wrong costs far more.
        low = normalize_question(question)
        if any(w in low for w in LEGAL_TRIPWIRES):
            raise HaltForInput(
                question,
                "Immigration/identity question not recognised by the taxonomy — "
                "refusing to guess or send it to a model",
            )

        stored = self._stored(question)
        if stored:
            return stored

        if qid:
            fromprofile = self._from_profile(qid, options)
            if fromprofile and fromprofile.ok:
                return fromprofile

        return Resolution(None, "unresolved", 0.0, qid)

    # ------------------------------------------------------------------ #
    def _resolve_legal(self, question: str, qid: str | None,
                       options: list[str] | None) -> Resolution:
        """Verbatim from profile. Never a model, never a fuzzy match."""
        wa = self.p.get("work_authorization", {})
        ans = wa.get("answers", {}) if isinstance(wa, dict) else {}

        if qid is None:
            raise HaltForInput(question, "Unclassified question matched legal heuristics")

        if qid == "sponsorship_future":
            return Resolution(
                ans.get("require_sponsorship", "Yes"), "profile_legal", 1.0, qid,
                needs_qualifier=True, qualifier_text=ans.get("require_sponsorship_qualifier", ""),
            )

        if qid == "authorized_now":
            # "Authorized to work" is always asked about a SPECIFIC country and
            # the answer differs by country. Resolving all of them with the US
            # answer would put a false "Yes" on a UK, EU, Canadian or Singapore
            # application. Detect the country, and refuse to guess.
            q = normalize_question(question)
            padded = " " + q + " "
            countries = [
                (("united states", "usa", " us ", "america"), "authorized_now_us"),
                (("united kingdom", " uk ", "britain", "england"), "authorized_now_uk"),
                (("canada", "canadian"), "authorized_now_canada"),
                (("singapore",), "authorized_now_singapore"),
                (("european union", " eu ", "europe", "eea", "schengen", "germany",
                  "netherlands", "ireland", "switzerland"), "authorized_now_eu"),
            ]
            hits = [key for needles, key in countries if any(n in padded for n in needles)]
            if len(hits) > 1:
                raise HaltForInput(question, "Names more than one country")
            if hits:
                key = hits[0]
                qual = ans.get(key + "_qualifier", "")
                if key != "authorized_now_us" and not qual:
                    qual = ans.get("non_us_qualifier", "")
                return Resolution(ans.get(key, "No"), "profile_legal", 1.0, qid,
                                  needs_qualifier=bool(qual), qualifier_text=qual)
            # No country named. On a US application this nearly always means the
            # US, and the configured default says so explicitly rather than the
            # resolver assuming it.
            if self.s.get("meta.default_work_country", "US") == "US":
                qual = ans.get("authorized_now_us_qualifier", "")
                return Resolution(ans.get("authorized_now_us", "Yes"), "profile_legal",
                                  1.0, qid, needs_qualifier=bool(qual), qualifier_text=qual)
            raise HaltForInput(question, "Work-authorisation question names no country")

        if qid == "citizenship":
            q = normalize_question(question)
            # "Are you a US citizen?" is a yes/no; "country of citizenship" is a value.
            if re.search(r"are you a|u s citizen|united states citizen", q):
                return Resolution("No" if not wa.get("us_citizen") else "Yes",
                                  "profile_legal", 1.0, qid)
            return Resolution(wa.get("citizenship"), "profile_legal", 1.0, qid)

        if qid == "visa_status":
            return Resolution(wa.get("visa_status"), "profile_legal", 1.0, qid)

        if qid in ("security_clearance", "export_control"):
            return Resolution("No", "profile_legal", 1.0, qid)

        if qid == "legal_name":
            ident = self.p.get("identity", {})
            q = normalize_question(question)
            if "first" in q:
                return Resolution(ident.get("first_name"), "profile_legal", 1.0, qid)
            if "last" in q or "family" in q or "surname" in q:
                return Resolution(ident.get("last_name"), "profile_legal", 1.0, qid)
            return Resolution(ident.get("full_name"), "profile_legal", 1.0, qid)

        if qid == "date_of_birth":
            dob = self.p.get("identity.date_of_birth", None)
            if not dob or str(dob).upper() == "ASK":
                raise HaltForInput(question, "Date of birth not set in profile")
            return Resolution(dob, "profile_legal", 1.0, qid)

        if qid == "graduation_date":
            edu = (self.p.get("education", []) or [{}])[0]
            return Resolution(edu.get("expected_graduation"), "profile_legal", 1.0, qid)

        if qid == "degree_type":
            edu = (self.p.get("education", []) or [{}])[0]
            return Resolution(edu.get("degree"), "profile_legal", 1.0, qid)

        raise HaltForInput(question, f"Legal field '{qid}' has no resolver")

    # ------------------------------------------------------------------ #
    def _stored(self, question: str) -> Resolution | None:
        key = normalize_question(question)
        row = self.db.one("SELECT * FROM answers WHERE question_key=?", (key,))
        if row:
            return Resolution(row["answer"], "stored", 1.0)
        # fuzzy fallback, non-legal only
        rows = self.db.q("SELECT * FROM answers WHERE is_legal=0")
        best, best_score = None, 0.0
        for r in rows:
            score = fuzz.token_set_ratio(key, r["question_key"]) / 100.0
            if score > best_score:
                best, best_score = r, score
        if best is not None and best_score >= self.threshold:
            return Resolution(best["answer"], "stored", best_score)
        return None

    # ------------------------------------------------------------------ #
    def _from_profile(self, qid: str, options: list[str] | None) -> Resolution | None:
        p = self.p
        ident = p.get("identity", {})
        addr = p.get("address", {})
        edu = (p.get("education", []) or [{}])[0]
        avail = p.get("availability", {})
        comp = p.get("compensation", {})
        demo = p.get("demographics", {})

        simple: dict[str, Any] = {
            "first_name": ident.get("first_name"),
            "last_name": ident.get("last_name"),
            "full_name": ident.get("full_name"),
            "email": ident.get("email"),
            "phone": ident.get("phone"),
            "linkedin": ident.get("linkedin"),
            "github": ident.get("github"),
            "website": ident.get("website"),
            "pronouns": ident.get("pronouns"),
            "address_line1": addr.get("line1"),
            "city": addr.get("city"),
            "state": addr.get("state"),
            "postal_code": addr.get("postal_code"),
            "country": addr.get("country"),
            "school": edu.get("institution"),
            "major": ", ".join(edu.get("majors", []) or []),
            "gpa": edu.get("gpa"),
            "start_date": avail.get("earliest_start"),
            "end_date": avail.get("latest_end"),
            "relocate": "Yes" if avail.get("willing_to_relocate") else "No",
            "salary": comp.get("answer_if_required"),
            "gender": demo.get("gender"),
            "ethnicity": demo.get("ethnicity"),
            "veteran": demo.get("veteran_status"),
            "disability": demo.get("disability_status"),
            "referred_by": "No",
            "previously_employed": "No",
            "how_heard": "Company website",
        }
        val = simple.get(qid)
        if val is None or (isinstance(val, str) and val.strip().upper() == "ASK"):
            return None
        if options:
            matched = best_option(str(val), options)
            if matched:
                return Resolution(matched, "profile", 0.95, qid)
        return Resolution(val, "profile", 0.95, qid)

    # ------------------------------------------------------------------ #
    def remember(self, question: str, answer: Any, *, field_type: str = "text",
                 source: str = "user", options: list[str] | None = None,
                 is_legal: bool = False) -> None:
        key = normalize_question(question)
        self.db.run(
            "INSERT INTO answers(question_key,question_text,answer,field_type,options_json,"
            "source,is_legal,created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(question_key) DO UPDATE SET answer=excluded.answer, "
            "question_text=excluded.question_text, source=excluded.source",
            (key, question, str(answer), field_type,
             json.dumps(options) if options else None, source, int(is_legal), now()),
        )

    def enqueue_ask(self, question: str, *, field_type: str = "text",
                    options: list[str] | None = None, job_id: int | None = None,
                    context: str = "") -> None:
        self.db.run(
            "INSERT OR IGNORE INTO ask_queue(question_text,field_type,options_json,job_id,"
            "context,created_at) VALUES(?,?,?,?,?,?)",
            (question, field_type, json.dumps(options) if options else None,
             job_id, context, now()),
        )

    def pending_asks(self) -> list[dict]:
        return [dict(r) for r in self.db.q(
            "SELECT * FROM ask_queue WHERE resolved_at IS NULL ORDER BY id")]

    def mark_used(self, question: str) -> None:
        self.db.run(
            "UPDATE answers SET times_used=times_used+1, last_used_at=? WHERE question_key=?",
            (now(), normalize_question(question)),
        )


def best_option(value: str, options: list[str]) -> str | None:
    """Map a free value onto the closest offered option (for selects/radios)."""
    if not options:
        return None
    v = value.strip().lower()
    for o in options:
        if o.strip().lower() == v:
            return o
    # yes/no shortcuts
    if v in ("yes", "true", "y"):
        for o in options:
            if o.strip().lower() in ("yes", "true"):
                return o
    if v in ("no", "false", "n"):
        for o in options:
            if o.strip().lower() in ("no", "false"):
                return o
    best, score = None, 0.0
    for o in options:
        s = fuzz.token_set_ratio(v, o.lower()) / 100.0
        if s > score:
            best, score = o, s
    return best if score >= 0.82 else None
