"""Regression tests for the legal/immigration answer resolver.

These exist because three real bugs were found here by testing rather than
reading, and every one of them would have put a false statement on a live
application:

  1. "Are you a U.S. citizen?" resolved to None. Punctuation stripping turned
     "U.S." into "u s", which matched no pattern, so the question fell through
     toward the model instead of the profile.
  2. A first attempt at fixing (1) collapsed any run of single letters, which
     ate the article in "are you a u s citizen" and produced "aus".
  3. "Are you authorized to work in Canada?" answered "Yes" — it matched the
     work-authorisation pattern and returned the *US* answer.

(3) is the one that matters most. A wrong answer here is not a bad application;
for a visa-dependent applicant the same statement reappears on government forms.
So the rule under test is: resolve exactly, or halt and ask. Never guess.
"""

from __future__ import annotations

import pytest

from applier.apply.answers import HaltForInput, classify, normalize_question


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected_contains", [
    ("Are you a U.S. citizen?", "us citizen"),
    ("Are you a U.S.A. citizen?", "usa citizen"),
    ("Right to work in the U.K.?", "uk"),
    ("Legal Name *", "legal name"),
    ("Phone number (required)", "phone number"),
])
def test_normalisation(raw, expected_contains):
    assert expected_contains in normalize_question(raw)


def test_article_is_not_swallowed():
    """The 'a' in 'are you a u s citizen' must survive."""
    out = normalize_question("Are you a U.S. citizen?")
    assert "aus" not in out
    assert out.startswith("are you a us")


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question,qid", [
    ("Are you a U.S. citizen?", "citizenship"),
    ("Are you a US citizen or national?", "citizenship"),
    ("What is your country of citizenship?", "citizenship"),
    ("Are you legally authorized to work in the United States?", "authorized_now"),
    ("Do you have the right to work in Singapore?", "authorized_now"),
    ("Will you now or in the future require sponsorship?", "sponsorship_future"),
    ("Do you require visa sponsorship?", "sponsorship_future"),
    ("Would you need immigration support at any point?", "sponsorship_future"),
    ("What is your current visa status?", "visa_status"),
    ("Do you hold a security clearance?", "security_clearance"),
    ("What is your date of birth?", "date_of_birth"),
])
def test_legal_questions_classify(question, qid):
    got, is_legal = classify(question)
    assert got == qid, f"{question!r} classified as {got}, expected {qid}"
    assert is_legal, f"{question!r} must be treated as a legal field"


def test_ordinary_questions_are_not_legal():
    for q in ("How did you hear about us?", "What is your expected salary?",
              "Are you willing to relocate?"):
        _, is_legal = classify(q)
        assert not is_legal, f"{q!r} wrongly treated as a legal field"


# --------------------------------------------------------------------------- #
# resolution
# --------------------------------------------------------------------------- #
@pytest.fixture
def bank(tmp_path):
    from applier.apply.answers import AnswerBank
    from applier.config import Config
    from applier.db import Database

    profile = Config({
        "identity": {"full_name": "Test User", "first_name": "Test", "last_name": "User",
                     "email": "t@example.com", "date_of_birth": "1900-01-01"},
        "work_authorization": {
            "citizenship": "Elbonia", "us_citizen": False, "visa_status": "F-1 student",
            "answers": {
                "authorized_now_us": "Yes",
                "authorized_now_uk": "No",
                "authorized_now_canada": "No",
                "authorized_now_eu": "No",
                "authorized_now_singapore": "No",
                "require_sponsorship": "Yes",
                "require_sponsorship_qualifier": "Not for this internship.",
                "non_us_qualifier": "I would need the appropriate permit.",
            },
        },
        "education": [{"degree": "Bachelor of Arts", "expected_graduation": "2028-05"}],
    }, "test-profile")
    settings = Config({"apply": {"universal_filler": {"confidence_threshold": 0.8}},
                       "meta": {"default_work_country": "US"}}, "test-settings")
    return AnswerBank(Database(tmp_path / "t.db"), profile, settings)


@pytest.mark.parametrize("question,expected", [
    ("Are you legally authorized to work in the United States?", "Yes"),
    ("Are you authorized to work in the US?", "Yes"),
    ("Are you authorized to work in Canada?", "No"),
    ("Are you authorized to work in the United Kingdom?", "No"),
    ("Do you have the right to work in Singapore?", "No"),
    ("Are you a U.S. citizen?", "No"),
    ("What is your country of citizenship?", "Elbonia"),
    ("What is your current visa status?", "F-1 student"),
    ("Do you hold a security clearance?", "No"),
    ("What is your date of birth?", "1900-01-01"),
])
def test_resolves_exactly(bank, question, expected):
    assert bank.resolve(question).value == expected


def test_non_us_authorisation_is_never_yes(bank):
    """The bug that motivated this file. Guard it explicitly."""
    for country in ("Canada", "the United Kingdom", "Germany", "Singapore", "Ireland"):
        r = bank.resolve(f"Are you authorized to work in {country}?")
        assert r.value == "No", f"{country} wrongly answered {r.value!r}"


def test_sponsorship_always_yes_with_qualifier(bank):
    for q in ("Will you now or in the future require sponsorship for employment visa status?",
              "Do you require visa sponsorship?",
              "Would you need immigration support at any point?"):
        r = bank.resolve(q)
        assert r.value == "Yes"
        assert r.needs_qualifier and r.qualifier_text


def test_unrecognised_legal_wording_halts(bank):
    """Better to stop and ask than to guess on an immigration question."""
    with pytest.raises(HaltForInput):
        bank.resolve("Please describe your nationality and passport situation.")


def test_multiple_countries_halts(bank):
    with pytest.raises(HaltForInput):
        bank.resolve("Are you authorized to work in the US or Canada?")


def test_legal_fields_never_come_from_a_model(bank):
    """Every legal answer must be sourced from the profile, verbatim."""
    for q in ("Are you a U.S. citizen?", "What is your current visa status?",
              "Are you legally authorized to work in the United States?"):
        assert bank.resolve(q).source == "profile_legal"
