"""Reading a posting correctly: who it will take, and what it is actually for.

The posting text below is paraphrased and its personal details are synthetic:
this file is tracked and the repository is public, so a real graduation date
or visa class in here is a leak. scripts/audit_privacy.py checks that, and it
caught this file doing exactly that on its first commit.

All four behaviours here come from one real application — AlphaSights' 2027
client-service resume drop — and each was a *silent* wrong answer rather than
an error. That is the pattern worth pinning:

* the posting says "we do not sponsor visas **but will consider applicants
  with STEM/OPT eligibility**", and reading only the first half of that
  sentence discards a job the candidate is explicitly invited to apply for;
* its title is "Summer Associate, Client Service", which matched no family, so
  it fell through to `default` and produced a resume led by finite-geometry
  research for a job about talking to people on the phone;
* Greenhouse called the employer `alphasightsresumedrop`, which is a board
  slug, not a company;
* and the rendered PDF's interword spaces were 3.07pt against pdfminer's 3.0pt
  word-split threshold, so the whole document sat a hair above the point where
  every word runs into the next.

None of these raised anything. The suite is where they stay fixed.
"""

from __future__ import annotations

import re

import pytest

from applier.score.gates import OPT_CARVE_OUT, check_gates
from applier.tailor.bank import job_family


# --------------------------------------------------------------------------- #
# a qualified refusal is not a refusal
# --------------------------------------------------------------------------- #
ALPHASIGHTS = (
    "Fluent Portuguese is required. Work authorization in the United States; we "
    "do not sponsor visas but will consider applicants with STEM/OPT "
    "eligibility. Internship compensation is $15,384.62 for a 10-week program."
)


class _Cfg:
    """Minimal stand-in for Config: check_gates only ever reads by dotted key."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, key, default=None):
        node = self._data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _settings(**over):
    gates = {
        "hard_reject_phrases": [
            "U.S. Citizen", "security clearance", "ITAR",
            "will not sponsor", "no visa sponsorship",
            "must be authorized to work in the United States without sponsorship",
        ],
        "sponsor_friendly_phrases": ["visa sponsorship is available"],
    }
    gates.update(over)
    return _Cfg({"gates": gates})


def _profile():
    return _Cfg({"education": [{"expected_graduation": "2031-06"}]})


def _job(desc, title="Summer Associate, Client Service, 2027"):
    return {"id": 1, "title": title, "description": desc, "company": "AlphaSights"}


@pytest.mark.parametrize("sentence", [
    "we do not sponsor visas but will consider applicants with STEM/OPT eligibility",
    "We will not sponsor employment visas, however candidates eligible for CPT "
    "or OPT are welcome to apply",
    "No visa sponsorship is offered, although F-1 visa holders on practical "
    "training will be considered",
])
def test_a_sponsorship_refusal_that_carves_out_opt_is_detected(sentence):
    assert OPT_CARVE_OUT.search(sentence), f"carve-out missed in: {sentence!r}"


@pytest.mark.parametrize("sentence", [
    "We do not sponsor visas.",
    "This role requires work authorization; we will not sponsor.",
    "No visa sponsorship is available for this position.",
    # The carve-out word is present but it qualifies something else entirely.
    "We do not sponsor visas. However, the role is remote.",
])
def test_an_unqualified_refusal_is_not_mistaken_for_a_carve_out(sentence):
    assert not OPT_CARVE_OUT.search(sentence), f"false carve-out in: {sentence!r}"


def test_the_alphasights_posting_is_not_rejected():
    """The whole point. This job was winnable and the gate nearly binned it."""
    g = check_gates(_job(ALPHASIGHTS),
                    _settings(hard_reject_phrases=["will not sponsor",
                                                   "no visa sponsorship",
                                                   "we do not sponsor visas"]),
                    _profile())
    assert g.passed, f"gate rejected a reachable job: {g.reason}"
    assert g.sponsorship == "opt_ok"


def test_the_carve_out_does_not_rescue_a_clearance_requirement():
    """Citizenship and clearance are never qualified. They must still reject."""
    desc = ("Applicants must hold an active security clearance. We do not "
            "sponsor visas but will consider applicants with STEM/OPT "
            "eligibility for other roles.")
    g = check_gates(_job(desc), _settings(), _profile())
    assert not g.passed and "clearance" in g.reason.lower()


def test_a_flat_refusal_still_rejects():
    desc = "Candidates must be authorized to work in the United States without sponsorship."
    g = check_gates(_job(desc), _settings(), _profile())
    assert not g.passed


def test_opt_ok_scores_above_silence_and_below_an_explicit_offer():
    from applier.score.gates import GateResult, score_job
    s = _settings()
    p = _profile()
    job = _job(ALPHASIGHTS)
    vals = {k: score_job(job, s, p, GateResult(True, "", k)).breakdown["sponsorship"]
            for k in ("sponsors", "opt_ok", "silent", "refuses")}
    assert vals["sponsors"] > vals["opt_ok"] > vals["silent"] > vals["refuses"]


def test_an_unknown_sponsorship_value_does_not_crash_scoring():
    """The column is free text in SQLite; a stale row must not take the ranker down."""
    from applier.score.gates import GateResult, score_job
    score_job(_job(ALPHASIGHTS), _settings(), _profile(),
              GateResult(True, "", "something-new"))


# --------------------------------------------------------------------------- #
# the job family
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", [
    "Summer Associate, Client Service, 2027 (Campus Resume Drop)",
    "Client Services Associate",
    "Business Analyst Intern",
    "Customer Success Intern",
    "Management Consulting Summer Analyst",
    "Business Development Representative",
])
def test_client_facing_titles_get_the_client_family(title):
    assert job_family({"title": title, "description": ""}) == "client"


@pytest.mark.parametrize("title,expected", [
    # A specialist qualifier always wins over the client reading.
    ("Security Consulting Analyst", "security"),
    ("Quantitative Trading Summer Associate", "quant"),
    ("Machine Learning Client Solutions Engineer", "ai_ml"),
    # And an engineering job billed to a client is still an engineering job:
    # this is why none of the client keywords is a bare "consultant".
    ("Software Engineering Consultant", "default"),
    ("Technical Consultant, Backend", "default"),
])
def test_a_specialist_title_is_not_captured_by_the_client_family(title, expected):
    assert job_family({"title": title, "description": ""}) == expected


def test_the_client_family_has_evidence_pointed_at_it():
    """A family nothing leads for is a family that changes nothing."""
    from pathlib import Path
    master = Path(__file__).resolve().parent.parent / "config" / "bank" / "master_resume.md"
    if not master.is_file():
        pytest.skip("master resume is local-only")
    leads = [ln for ln in master.read_text(encoding="utf-8").splitlines()
             if ln.startswith("lead_for:") and "client" in ln]
    assert len(leads) >= 3, (
        "fewer than three entries lead for client work; a client-service "
        "posting will fall back to whatever is strongest technically")


# --------------------------------------------------------------------------- #
# a board token is not a company name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,clean", [
    ("AlphaSights Campus Recruiting", "AlphaSights"),
    ("Jane Street Early Careers", "Jane Street"),
    ("Acme Corp University Recruiting", "Acme Corp"),
    ("Foo Industries Careers", "Foo Industries"),
    ("Bar Resume Drop", "Bar"),
    ("Plain Company", "Plain Company"),
])
def test_board_names_shed_their_recruiting_suffixes(raw, clean):
    from applier.discover.sources import _BOARD_NAME_NOISE
    name, prev = raw, None
    while name and name != prev:
        prev = name
        name = _BOARD_NAME_NOISE.sub("", name).strip(" -–—|,")
    assert name == clean


def test_the_company_name_never_becomes_empty():
    """Stripping must not eat a company that IS called Careers or Talent."""
    from applier.discover.sources import _BOARD_NAME_NOISE
    for raw in ("Careers", "Talent", "Recruiting"):
        name, prev = raw, None
        while name and name != prev:
            prev = name
            name = _BOARD_NAME_NOISE.sub("", name).strip(" -–—|,")
        # It may strip to nothing -- the caller must fall back to the token.
        assert name == "" or name == raw


# --------------------------------------------------------------------------- #
# the other shape, and the commoner one
# --------------------------------------------------------------------------- #
# OPT_CARVE_OUT reads a refusal that is immediately qualified, which needs a
# "but" to hang on. Most postings that take international students have no
# refusal to qualify -- they simply say so. Found when a test posting saying
# "we welcome international students and support CPT and OPT" scored as
# silent, below a posting that says nothing but happens to use a configured
# phrase verbatim.
@pytest.mark.parametrize("sentence", [
    "We welcome international students and support CPT and OPT.",
    "We accept students eligible for CPT/OPT.",
    "F-1 visa holders on practical training are encouraged to apply.",
    "International students are welcome to apply.",
    "Candidates eligible for CPT are considered.",
])
def test_a_posting_that_simply_welcomes_opt_is_read_as_such(sentence):
    from applier.score.gates import welcomes_opt
    assert welcomes_opt(sentence) is True


@pytest.mark.parametrize("sentence", [
    "We do not accept OPT candidates.",
    "We cannot support CPT at this time.",
    "We will not consider international students.",
    "International students are not eligible for this programme.",
    "No visa sponsorship is available.",
    "A great place to work, with free lunch.",
])
def test_a_refusal_is_not_read_as_a_welcome(sentence):
    """Every token this looks for sits inside "we do not accept OPT"."""
    from applier.score.gates import welcomes_opt
    assert welcomes_opt(sentence) is False


@pytest.mark.parametrize("desc,expected", [
    ("We do not sponsor visas but will consider applicants with STEM/OPT "
     "eligibility.", "opt_ok"),
    ("We welcome international students and support CPT and OPT.", "opt_ok"),
    ("F-1 visa holders on practical training are encouraged to apply.", "opt_ok"),
    ("A great place to work.", "silent"),
    ("International students are not eligible.", "silent"),
])
def test_the_gate_reaches_the_same_answer_whichever_shape_it_is(desc, expected):
    g = check_gates(_job(desc), _settings(), _profile())
    assert g.passed is True
    assert g.sponsorship == expected
