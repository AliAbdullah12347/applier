"""What the cover letter is argued from, and what gets checked before it ships.

Two defects, both of which produced a letter that read fine and was wrong.

* **The evidence was `bank.atoms[:22]`** -- the first atoms in the master
  file, in the order they were written. Whether the model ever saw the
  relevant project depended on where that project happened to sit in the
  file, so a client-service posting got a letter argued from finite-geometry
  research. The resume had been fixed to select by relevance months before;
  the letter was still reading off the top of the file.

* **The draft shipped as written.** `audit_generated` ran, found whatever it
  found, wrote it to `cover_letter_flags.json`, and the letter went into the
  application anyway. A check whose result nobody reads is not a check.

The critic here is deliberately not a model. A model asked to grade its own
letter agrees with itself; a word count does not. Everything it reports is
phrased as the correction to make, because that is what is handed back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applier.config import Config
from applier.tailor import pipeline as P
from applier.tailor.bank import Bank
from applier.tailor.master import build_bank

MASTER = """## Skills

Languages: Python, Rust

## Experience

### Sits First In The File
id: exp_first
dates: 2026
tags: knitting, pottery

- Catalogued the studio kiln schedule by hand
  ~ Catalogued the studio kiln schedule by hand.
  ~~ Catalogued the kiln schedule.

### Answers The Posting
id: exp_match
dates: 2026
tags: python, kubernetes, distributed

- Built Python services on Kubernetes across a distributed cluster
  ~ Built Python services on Kubernetes across a distributed cluster.
  ~~ Built distributed services.
"""

PROFILE = Config({
    "identity": {"full_name": "Jane Doe", "email": "jane@example.com",
                 "phone": "+1 555-0100", "github": "github.com/example",
                 "website": "example.com"},
    "education": [{"gpa": "3.90", "gpa_scale": "4.00"}],
    "availability": {"earliest_start": "2030-05", "latest_end": "2030-08"},
    "voice": {"tone": "direct", "cover_letter_words": [60, 200],
              "avoid_phrases": ["kindly find enclosed", "a natural fit"],
              "prefer": ["name the actual technology"]},
})

JOB = {
    "id": 1,
    "company": "Northwind",
    "title": "Software Engineering Intern",
    "description": "Northwind runs python services on kubernetes across a "
                   "distributed cluster. You will ship backend features.",
}

CLEAN = (
    "Northwind runs python services on kubernetes, which is the part of the "
    "posting that made me write. The cluster problems you describe are the "
    "ones I have spent the last year on.\n\n"
    "The scheduler I wrote assumed every node answered within a second, and "
    "under real load a third of them did not. Rewriting it around deadlines "
    "rather than timeouts removed the whole class of failure, and taught me "
    "to distrust any distributed design whose correctness rests on things "
    "being fast.\n\n"
    "I learned Rust that same year for the same reason, wanting a language "
    "that would not let a shared buffer be written from two places at once. "
    "That habit of pushing an invariant into the type system is what I would "
    "bring to the backend work you describe here.\n\n"
    "I am available from late May to August 2030."
)


@pytest.fixture
def bank(tmp_path) -> Bank:
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    return Bank(tmp_path)


def _problems(body: str, bank: Bank, *, resume_lines: list[str] | None = None,
              job: dict | None = None) -> list[str]:
    return P._letter_problems(body, bank=bank, profile=PROFILE,
                              job=job or JOB, words=(60, 200),
                              resume_lines=resume_lines or [])


# --------------------------------------------------------------------------- #
# what the letter is argued from
# --------------------------------------------------------------------------- #
def test_evidence_is_ranked_by_the_posting_not_by_file_order(bank):
    printed, spare = P._letter_evidence(bank, JOB, None)
    first = (printed + spare)[0]
    assert "Kubernetes" in first, (
        "the posting is about Kubernetes; the letter must not be handed the "
        f"top of the file instead. got: {first}")


def test_evidence_says_which_bullets_the_resume_already_prints(bank):
    sel = bank.select(JOB["description"], family="default", line_budget=4)
    printed, spare = P._letter_evidence(bank, JOB, sel)
    on_page = {a.id for a, _ in sel.atoms}
    assert on_page, "the fixture should put something on the page"
    assert printed, "what the resume prints must reach the prompt"
    assert not (set(printed) & set(spare)), "a bullet is on the page or it is not"


def test_with_no_resume_every_bullet_is_offered_as_fresh(bank):
    printed, spare = P._letter_evidence(bank, JOB, None)
    assert printed == []
    assert spare


# --------------------------------------------------------------------------- #
# the critic
# --------------------------------------------------------------------------- #
def test_a_good_letter_has_nothing_to_fix(bank):
    assert _problems(CLEAN, bank) == []


def test_too_short_is_reported_with_the_floor(bank):
    out = _problems("Northwind is interesting.\n\nI did things.\n\nThanks.", bank)
    assert any("at least 60" in p for p in out)


def test_a_banned_phrase_is_reported_by_name(bank):
    out = _problems(CLEAN.replace("which is the part of the posting that made "
                                  "me write", "kindly find enclosed to join"), bank)
    assert any("kindly find enclosed" in p for p in out)


def test_a_filler_opener_is_reported(bank):
    out = _problems(CLEAN.replace("The scheduler I wrote",
                                  "Furthermore, the scheduler I wrote"), bank)
    assert any("furthermore" in p.lower() for p in out)


def test_mentioning_sponsorship_is_reported(bank):
    out = _problems(CLEAN + " I will need visa sponsorship.", bank)
    assert any("sponsorship" in p.lower() or "visa" in p.lower() for p in out)


def test_markdown_is_reported(bank):
    out = _problems("Northwind.\n\n- a bullet\n- another\n\nClose.\n\nEnd.", bank)
    assert any("markdown" in p.lower() or "bullets" in p.lower() for p in out)


def test_an_iso_date_is_reported(bank):
    out = _problems(CLEAN.replace("late May to August 2030", "2030-05 to 2030-08"),
                    bank)
    assert any("ISO" in p for p in out)


def test_an_opening_that_never_names_the_company_is_reported(bank):
    out = _problems(CLEAN.replace("Northwind runs", "This company runs"), bank)
    assert any("never names Northwind" in p for p in out)


BULLET = "Built Python services on Kubernetes across a distributed cluster."

OPEN = "Northwind runs kubernetes and I have worked on it for a year.\n\n"
CLOSE = "\n\nI am available from late May to August 2030."
DEPTH = (
    "The scheduler assumed every node answered within a second, and under "
    "real load a third of them did not. Rewriting it around deadlines rather "
    "than timeouts removed the whole class of failure, and taught me to "
    "distrust any design whose correctness rests on things being fast."
)
ASIDE = (
    "I learned Rust that year wanting a language that would not let a shared "
    "buffer be written from two places at once, and that habit of pushing an "
    "invariant into the type system is what I would bring to the work you "
    "describe."
)


def test_a_paragraph_that_only_repeats_a_bullet_is_reported(bank):
    body = OPEN + BULLET + "\n\n" + DEPTH + CLOSE
    out = _problems(body, bank, resume_lines=[BULLET])
    assert any("reproduces a resume bullet" in p for p in out), out


def test_naming_the_bullet_and_then_going_deeper_is_not_restatement(bank):
    """The regression the sentence-level check caused.

    An evidence paragraph is *asked* to name its item, so its first sentence
    always echoes the bullet. Scoring that sentence on its own condemned a
    good paragraph for its opening line, and the revise loop spent a round
    paraphrasing a sentence that was doing its job.
    """
    body = OPEN + BULLET + " " + DEPTH + "\n\n" + ASIDE + CLOSE
    out = _problems(body, bank, resume_lines=[BULLET])
    assert not any("resume bullet" in p for p in out), out


def test_a_paragraph_built_on_several_items_at_once_is_reported(bank):
    """How "do not recite the resume" actually fails: a paragraph that is a list."""
    bullets = [
        BULLET,
        "Designed a provider-agnostic router across ten providers with fallbacks.",
        "Wrote eighty automated tests across three separate suites.",
    ]
    body = OPEN + " I also ".join(bullets) + "\n\n" + ASIDE + CLOSE
    out = _problems(body, bank, resume_lines=bullets)
    assert any("different items at once" in p for p in out), out


def test_a_paragraph_opening_word_is_not_an_invented_proper_noun(bank):
    """`audit_generated` reported "When" as a name it could not trace.

    Its sentence-initial guard required a full stop followed by exactly one
    space, so it saw sentences inside a paragraph and missed every sentence
    that opened one -- those follow a blank line. The revise loop then spent
    a round trying to remove the word "When" from the letter.
    """
    body = (OPEN
            + "When the scheduler assumed every node answered within a second, "
              "under real load a third of them did not, and rewriting it around "
              "deadlines removed the whole class of failure.\n\n"
            + "While learning Rust that same year I wanted a language that "
              "would not let a shared buffer be written from two places at "
              "once, which is the habit I would bring here."
            + CLOSE)
    out = _problems(body, bank)
    assert not any("proper_noun" in p for p in out), out


def test_an_invented_number_is_reported(bank):
    out = _problems(CLEAN.replace("a third of them did not",
                                  "417 of them did not"), bank)
    assert any("417" in p for p in out)


def test_every_problem_tells_the_model_what_to_do(bank):
    """The list is handed back as instructions, so it has to read as one."""
    out = _problems("Northwind. kindly find enclosed.\n\n2030-05.\n\n- x", bank)
    assert out
    verbs = ("remove", "cut", "add", "use", "merge", "open", "delete", "write",
             "say", "replace")
    for item in out:
        assert any(v in item.lower() for v in verbs), f"not actionable: {item}"


# --------------------------------------------------------------------------- #
# the revise loop
# --------------------------------------------------------------------------- #
class FakeRouter:
    """Returns the queued bodies in order and counts the calls."""

    def __init__(self, bodies: list[str]) -> None:
        self.bodies = list(bodies)
        self.calls: list[str] = []

    def json(self, system: str, user: str, *, task: str = "") -> dict:
        self.calls.append(user)
        return {"body": self.bodies.pop(0) if self.bodies else ""}


def _build(router, tmp_path, bank) -> Path | None:
    return P.build_cover_letter(JOB, Config({}), PROFILE, router, tmp_path, bank)


def test_a_clean_draft_costs_exactly_one_call(bank, tmp_path):
    router = FakeRouter([CLEAN])
    path = _build(router, tmp_path, bank)
    assert path and path.exists()
    assert len(router.calls) == 1
    assert not (tmp_path / "cover_letter_review.json").exists()


def test_a_defective_draft_is_sent_back_with_its_defects(bank, tmp_path):
    bad = CLEAN.replace("which is the part of the posting that made me write",
                        "kindly find enclosed to join")
    router = FakeRouter([bad, CLEAN])
    path = _build(router, tmp_path, bank)
    assert path
    assert len(router.calls) == 2, "the draft should have been revised"
    assert "kindly find enclosed" in router.calls[1], (
        "the revision prompt must name the defect it is fixing")
    assert "kindly find enclosed" not in path.read_text(encoding="utf-8")


def test_a_revision_that_does_not_help_is_refused(bank, tmp_path):
    """A model asked to fix four things can introduce a fifth."""
    bad = CLEAN.replace("which is the part of the posting that made me write",
                        "kindly find enclosed to join")
    worse = bad + "\n\nFurthermore, I am a a natural fit.\n\nMore.\n\nStill more."
    router = FakeRouter([bad, worse])
    path = _build(router, tmp_path, bank)
    assert path
    text = path.read_text(encoding="utf-8")
    assert "a natural fit" not in text, "the worse revision was shipped"
    assert "kindly find enclosed" in text, "the draft should have been kept"
    review = tmp_path / "cover_letter_review.json"
    assert review.exists() and "rejected" in review.read_text(encoding="utf-8")


def test_what_could_not_be_fixed_is_written_down(bank, tmp_path):
    bad = "Northwind. kindly find enclosed.\n\nShort.\n\nEnd."
    router = FakeRouter([bad, bad, bad])
    path = _build(router, tmp_path, bank)
    assert path, "an imperfect letter still ships -- quietly losing it is worse"
    review = tmp_path / "cover_letter_review.json"
    assert review.exists()
    assert "unresolved" in review.read_text(encoding="utf-8")


def test_a_router_failure_mid_revision_keeps_the_draft(bank, tmp_path):
    class Flaky(FakeRouter):
        def json(self, system, user, *, task=""):
            if len(self.calls) >= 1:
                self.calls.append(user)
                raise RuntimeError("upstream 503")
            return super().json(system, user, task=task)

    bad = CLEAN.replace("which is the part of the posting that made me write",
                        "kindly find enclosed to join")
    path = _build(Flaky([bad]), tmp_path, bank)
    assert path and "kindly find enclosed" in path.read_text(encoding="utf-8")


def test_the_letter_is_named_after_the_person_and_the_job(bank, tmp_path):
    path = _build(FakeRouter([CLEAN]), tmp_path, bank)
    assert path.name == "Jane Doe - Northwind - Software Engineering Intern - Cover Letter.txt"
