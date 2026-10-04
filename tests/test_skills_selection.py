"""What goes in the skills block, and what the block costs.

The old block printed every group the master file declared, with a bold label
each, on every application. Eleven lines on a one-page resume -- four or five
bullets' worth -- spent largely on work the page never mentioned. A
client-service application carried "Graphics: Unreal Engine 5, Blueprints,
Blender, HLSL"; a backend one carried cell-type deconvolution.

The rule now is that a skill has to be earned, by one of three routes, and
these tests pin each route and its limit. The limit matters more than the
route: every one of these tiers is a way of saying "this is relevant", and a
tier with no ceiling turns back into "print everything".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applier.tailor.bank import Bank, mentions
from applier.tailor.master import build_bank

ROOT = Path(__file__).resolve().parent.parent

MASTER = """## Skills

Languages: Python, Rust, C
Frontend: React, Svelte
Support: ticketing, escalation, hardware and software troubleshooting
Niche: CIBERSORT, quantum annealing

## Experience

### Service Desk
id: exp_desk
dates: 2026
tags: support, troubleshooting, hardware, software, ticketing, escalation

- Answered calls and walked users through faults
  ~ Answered calls and walked users through faults.
  ~~ Answered calls.
- Reproduced reported problems before escalating
  ~ Reproduced reported problems before escalating.
  ~~ Reproduced problems.
- Documented every interaction end to end
  ~ Documented every interaction end to end.
  ~~ Documented interactions.
- Coordinated issues across several teams
  ~ Coordinated issues across several teams.
  ~~ Coordinated across teams.

### Backend Work
id: exp_backend
dates: 2026
tags: python, backend

- Built services in Python against a REST API
  ~ Built services in Python against a REST API.
  ~~ Built Python services.
"""


@pytest.fixture
def bank(tmp_path) -> Bank:
    m = tmp_path / "master.md"
    m.write_text(MASTER, encoding="utf-8")
    build_bank(m, tmp_path, write=True)
    return Bank(tmp_path)


def _skills(bank: Bank, jd: str, **kw) -> list[str]:
    sel = bank.select(jd, family="default", line_budget=40)
    return bank.skills_for(jd, sel, **kw)


# --------------------------------------------------------------------------- #
# the three routes in
# --------------------------------------------------------------------------- #
def test_a_skill_the_posting_asks_for_is_printed_first(bank):
    out = _skills(bank, "We need strong Rust. You will write Rust daily.")
    assert "Rust" in out
    assert out[0] == "Rust", f"the posting's own word should lead: {out[:4]}"


def test_a_skill_a_bullet_demonstrates_is_printed(bank):
    out = _skills(bank, "Backend engineering role.")
    assert "Python" in out, "a bullet on the page says Python"


def test_a_skill_nothing_supports_is_dropped(bank):
    out = _skills(bank, "Backend engineering role.")
    assert "quantum annealing" not in out, (
        "printed a skill the posting never asks for and no bullet shows")


def test_an_entrys_tags_can_vouch_for_a_skill_its_bullets_never_say(bank):
    """The case this tier exists for: the desk is tagged `ticketing`."""
    out = _skills(bank, "Client support role handling inbound problems.")
    assert "ticketing" in out and "escalation" in out


def test_tags_only_vouch_when_the_entry_actually_occupies_the_page(bank):
    """One pinned bullet is not a claim to a specialism.

    A quantitative-trading resume ended with "macOS, Windows, iOS, Android,
    ticketing, escalation" because the service desk contributed a single
    pinned bullet and its tag list was taken at face value.
    """
    from applier.tailor.bank import TAG_VOUCH_MIN_BULLETS

    jd = "Backend engineering role."
    sel = bank.select(jd, family="default", line_budget=40)
    counts: dict[str, int] = {}
    for atom, _ in sel.atoms:
        counts[atom.group] = counts.get(atom.group, 0) + 1

    out = bank.skills_for(jd, sel)
    if counts.get("exp_desk", 0) < TAG_VOUCH_MIN_BULLETS:
        assert "ticketing" not in out, (
            "a barely-present entry vouched for a skill no bullet mentions")


# --------------------------------------------------------------------------- #
# the limits
# --------------------------------------------------------------------------- #
def test_the_cap_is_honoured(bank):
    out = _skills(bank, "Python Rust React support ticketing escalation", cap=3)
    assert len(out) <= 3


def test_the_floor_keeps_the_section_from_collapsing(bank):
    """A vague posting must not produce an empty skills block."""
    out = _skills(bank, "A good opportunity for a motivated person.", floor=4)
    assert len(out) >= 4


def test_no_duplicates(bank):
    out = _skills(bank, "Python Python python PYTHON React")
    assert len(out) == len({s.lower() for s in out})


# --------------------------------------------------------------------------- #
# matching boundaries -- the reason this is not a substring search
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,skill,expected", [
    ("linux kernel work", "C", False),        # not the C inside "kernel"
    ("we use c and rust", "C", True),
    ("react hooks", "R", False),              # not the R inside "React"
    ("django orm", "Go", False),              # not the Go inside "Django"
    ("built in c# daily", "C#", True),
    ("scikit-learn pipelines", "C#", False),
    ("real-time rendering pipelines", "real-time rendering", True),
    ("sql server", "SQL", True),
])
def test_short_skill_names_need_a_real_boundary(text, skill, expected):
    assert mentions(text, skill) is expected


@pytest.mark.parametrize("skill,tags,expected", [
    ("hardware and software troubleshooting",
     "support troubleshooting hardware software", True),
    ("ticketing", "ticketing escalation", True),
    ("computer vision", "support troubleshooting hardware", False),
    # One shared word is not enough, or a `python` tag would vouch for
    # anything with Python in its name.
    ("PyTorch", "python algorithms", False),
])
def test_a_tag_must_cover_every_word_of_the_skill(skill, tags, expected):
    from applier.tailor.bank import _all_words_tagged
    assert _all_words_tagged(skill, tags) is expected


# --------------------------------------------------------------------------- #
# the page itself
# --------------------------------------------------------------------------- #
def test_the_template_prints_one_flat_list_with_no_group_labels():
    tpl = (ROOT / "templates" / "resume" / "classic.tex.j2").read_text(encoding="utf-8")
    assert "skills|join" in tpl, "skills are no longer rendered as one flat list"
    assert "for group, items in skills.items()" not in tpl, (
        "the labelled group loop is back; it cost eleven lines a resume")


def test_leadership_comes_before_projects():
    """It was last, and it carries the extracurricular several postings name."""
    tpl = (ROOT / "templates" / "resume" / "classic.tex.j2").read_text(encoding="utf-8")
    assert tpl.index("\\section{Leadership}") < tpl.index("\\section{Projects}")


def test_verification_cannot_demand_a_skill_the_page_no_longer_prints():
    """`expect_keywords` must come from what was printed, not from the bank.

    Taking them from the bank while trimming the printed list would have made
    verification fail on every render, for a resume that was entirely sound.
    """
    src = (ROOT / "applier" / "tailor" / "pipeline.py").read_text(encoding="utf-8")
    assert "_claimed_keywords(skills)" in src
    assert "def _claimed_keywords(skills: list[str])" in src
