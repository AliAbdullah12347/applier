"""How a one-page resume spends its lines, and what the reader sees first.

Three defects, all of them visible on the rendered page and none of them
visible in a diff:

* **Fit order reached the page.** `select()` fits pinned atoms first so that
  non-negotiables claim their space before anything competes for it. That is
  right for fitting and wrong for reading: the pinned entry then printed
  first regardless of the posting. A backend software posting came back led
  by finite-geometry research, and a real-time graphics posting by a
  university help desk, in both cases because that entry carried a pin.

* **Size was chosen by importance.** The most relevant atom was printed in
  its longest phrasing, so the better a resume matched, the worse it read --
  a graphics posting produced thirteen two-line bullets out of sixteen.

* **Cost was measured on the template.** A bullet citing a number carries a
  `{{C.path}}` slot of thirty-odd characters that renders as "216x", so
  bullets were charged lines they never used and the page stopped short of
  the margin with the budget reported as spent.

`scripts/check_families.py` renders one resume per family and prints the wrap
count, the page fill and the leading entry; it is the check these tests were
written from.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from applier.tailor.bank import (
    SHORTEN_KEEPS, SUPPORTING_CAP, WRAP_COLUMN, Bank, date_key)
from applier.tailor.master import build_bank


def _pad(stem: str, width: int) -> str:
    """A phrasing of an exact rendered width, so a test can state the width."""
    filler = " across every supported configuration and environment"
    text = stem
    while len(text) < width:
        text += filler
    return text[:width].rstrip() + "." * max(0, width - len(text[:width].rstrip()))


# Two tags, two hits, plus the experience bonus puts relevance above the 0.8
# the fuller phrasing needs; one tag that the posting never says keeps the
# distractor below it.
MASTER = f"""## Skills

Languages: Python

## Experience

### Pinned But Irrelevant
id: exp_pinned
dates: 2026
tags: curling, philately

- {_pad("Organised the club ladder", 90)}   [pin]
  ~ {_pad("Organised the club ladder", 80)}
  ~~ {_pad("Organised the ladder", 60)}

### Answers The Posting
id: exp_match
dates: 2026
tags: python, kubernetes

- {_pad("Built Python services on Kubernetes", 100)}
  ~ {_pad("Built Python services on Kubernetes", 90)}
  ~~ {_pad("Built services", 50)}

### Wraps Unless Shortened
id: exp_wraps
dates: 2026
tags: python, kubernetes

- {_pad("Ran Python workloads on Kubernetes", 200)}
  ~ {_pad("Ran Python workloads on Kubernetes", WRAP_COLUMN + 7)}
  ~~ {_pad("Ran Python workloads on Kubernetes", WRAP_COLUMN - 20)}

### Shortening Would Gut It
id: exp_gutted
dates: 2026
tags: python, kubernetes

- {_pad("Scaled Python workloads on Kubernetes", 200)}
  ~ {_pad("Scaled Python workloads on Kubernetes", WRAP_COLUMN + 7)}
  ~~ {_pad("Scaled workloads", 30)}
"""

JD = "we run python services on kubernetes"


@pytest.fixture
def bank(tmp_path) -> Bank:
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    return Bank(tmp_path)


def _chosen(bank: Bank, jd: str = JD, budget: int = 60) -> dict[str, str]:
    sel = bank.select(jd, family="default", line_budget=budget)
    return {atom.id: size for atom, size in sel.atoms}


def _order(bank: Bank, jd: str = JD, budget: int = 60) -> list[str]:
    sel = bank.select(jd, family="default", line_budget=budget)
    seen: list[str] = []
    for atom, _ in sel.atoms:
        if atom.group not in seen:
            seen.append(atom.group)
    return seen


# --------------------------------------------------------------------------- #
# a pin is a reason to appear, not a reason to go first
# --------------------------------------------------------------------------- #
def test_the_posting_decides_what_leads_the_page(bank):
    assert _order(bank)[0] == "exp_match"


def test_the_pinned_entry_still_appears(bank):
    # Ordering it honestly must not drop it: that would trade one silent
    # failure for a louder one.
    assert "exp_pinned" in _order(bank)


def test_an_irrelevant_pin_does_not_outrank_a_relevant_entry(bank):
    order = _order(bank)
    assert order.index("exp_match") < order.index("exp_pinned")


# --------------------------------------------------------------------------- #
# lines, not importance
# --------------------------------------------------------------------------- #
def test_a_relevant_bullet_is_not_printed_in_a_phrasing_that_wraps(bank):
    chosen = _chosen(bank)
    text, _ = bank.resolve(
        next(a.text(chosen[a.id]) for a in bank.atoms if a.id in chosen
             and a.group == "exp_wraps"),
        strict=False,
    )
    assert len(text) <= WRAP_COLUMN


def test_shortening_is_refused_when_it_would_gut_the_bullet(bank):
    # The stub keeps under SHORTEN_KEEPS of the sentence, so the second line
    # is the better trade. Stopping wraps is the goal; stripping the page of
    # evidence is not.
    chosen = _chosen(bank)
    picked = [a for a in bank.atoms if a.id in chosen and a.group == "exp_gutted"]
    assert picked, "the entry should still be selected"
    assert all(chosen[a.id] == "medium" for a in picked)


def test_the_fullest_phrasing_is_taken_only_when_it_costs_no_extra_line(bank):
    chosen = _chosen(bank)
    match = [a for a in bank.atoms if a.id in chosen and a.group == "exp_match"]
    assert match and all(chosen[a.id] == "long" for a in match)
    for atom in match:
        text, _ = bank.resolve(atom.text("long"), strict=False)
        assert len(text) <= WRAP_COLUMN


def test_every_printed_bullet_fits_a_line_or_keeps_its_content(bank):
    for atom, size in bank.select(JD, family="default", line_budget=60).atoms:
        text, _ = bank.resolve(atom.text(size), strict=False)
        if len(text) <= WRAP_COLUMN:
            continue
        for smaller in ("medium", "short"):
            if not bank.renderable(atom, smaller):
                continue
            alt, _ = bank.resolve(atom.text(smaller), strict=False)
            if len(alt) <= WRAP_COLUMN:
                assert len(alt) < len(text) * SHORTEN_KEEPS, (
                    f"{atom.id} wraps although {smaller} would fit and keeps "
                    f"{len(alt) / len(text):.0%} of it"
                )


# --------------------------------------------------------------------------- #
# cost is what reaches the page
# --------------------------------------------------------------------------- #
def test_a_claim_slot_is_charged_at_the_width_it_renders(tmp_path):
    """`{{C.x}}` is source, not ink."""
    master = tmp_path / "master.md"
    master.write_text(
        "## Skills\n\nLanguages: Python\n\n## Experience\n\n"
        "### Cites A Number\nid: exp_num\ndates: 2026\ntags: python\n\n"
        f"- Cut the run from 3 days to 20 minutes{' and kept it there' * 3}\n"
        "  ~ Cut the run from 3 days to 20 minutes.\n"
        "  ~~ Cut the run.\n",
        encoding="utf-8",
    )
    build_bank(master, tmp_path, write=True)
    b = Bank(tmp_path)
    atom = next(a for a in b.atoms if a.group == "exp_num")

    template = atom.text("medium")
    rendered, used = b.resolve(template, strict=False)
    assert used, "the numbers should have become claims"
    assert len(template) > len(rendered), "the slots should be wider than the ink"
    assert b._rendered_len(atom, "medium") == len(rendered)
    assert b._cost(atom, "medium", {"exp_num"}) == 1 + len(rendered) // WRAP_COLUMN


# --------------------------------------------------------------------------- #
# a section floor, and no atom twice
# --------------------------------------------------------------------------- #
FLOORED = """## Skills

Languages: Python

## Experience

### Answers The Posting
id: exp_match
dates: 2026
tags: python, kubernetes

- Built Python services on Kubernetes that scaled
  ~ Built Python services on Kubernetes.
  ~~ Built services.
- Ran Python workloads on Kubernetes in production
  ~ Ran Python workloads on Kubernetes.
  ~~ Ran workloads.
- Tuned Python services on Kubernetes under load
  ~ Tuned Python services on Kubernetes.
  ~~ Tuned services.

## Education

### A Degree
id: edu_degree
dates: 2028
tags: olympiad, mathematics

- Gold medallist in a national mathematics olympiad
  ~ Gold medallist in a national mathematics olympiad.
  ~~ Olympiad gold medallist.
"""


@pytest.fixture
def floored(tmp_path) -> Bank:
    master = tmp_path / "master.md"
    master.write_text(FLOORED, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    b = Bank(tmp_path)
    b.sections["education"]["min_atoms"] = 1
    return b


def test_a_section_floor_survives_a_posting_that_never_mentions_it(floored):
    """`min_atoms` sat in the config unread until the page filled up.

    An olympiad medal scores nothing against a backend posting, so greedy
    relevance drops it for one more project bullet. Relevance is right about
    order and wrong about presence: a resume with nothing under the degree
    line is not the better resume.
    """
    sel = floored.select("python kubernetes services", family="default",
                         line_budget=6)
    assert any(a.section == "education" for a, _ in sel.atoms)


def test_the_floor_does_not_print_an_atom_twice(floored):
    sel = floored.select("python kubernetes services olympiad mathematics",
                         family="default", line_budget=60)
    ids = [a.id for a, _ in sel.atoms]
    assert len(ids) == len(set(ids)), f"repeated: {ids}"


def test_a_floor_larger_than_the_section_does_not_loop(floored):
    floored.sections["education"]["min_atoms"] = 99
    sel = floored.select("python", family="default", line_budget=60)
    ids = [a.id for a, _ in sel.atoms]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# dates descend, because a stranger audits them on sight
# --------------------------------------------------------------------------- #
# Ordering Experience by relevance was right about which role answers the
# posting and wrong about what a resume is. A graphics posting printed Aug
# 2024, May 2025, Jan 2026, Aug 2026 -- oldest first, which reads not as
# untidy but as a sorted list sorted backwards. The dates are the one part of
# a resume a reader is qualified to check before reading a word of it.
#
# Projects keep relevance order: a project is a thing made, not a post held,
# and nobody audits a portfolio for chronology.
DATED = """## Experience

### Still Running
id: exp_now
dates: Aug 2026 -- Present
tags: curling

- Ran the thing that is still running.

### Ended Recently
id: exp_recent
dates: Jan 2026 -- May 2026
tags: python, kubernetes

- Ran Python services on Kubernetes until May.

### A Single Term
id: exp_term
dates: Fall 2025
tags: python

- Taught the Python course for one term.

### Long Ago
id: exp_old
dates: Jun 2022
tags: python, kubernetes

- Ran Python services on Kubernetes years ago.

## Projects

### Old But Relevant
id: project_old
dates: 2022
tags: python, kubernetes

- Built Python services on Kubernetes.

### New But Not
id: project_new
dates: 2026
tags: curling

- Built something about curling.
"""


@pytest.fixture
def dated(tmp_path) -> Bank:
    master = tmp_path / "dated.md"
    master.write_text(DATED, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    return Bank(tmp_path)


def _section_order(bank: Bank, section: str, jd: str = JD) -> list[str]:
    sel = bank.select(jd, family="default", line_budget=60)
    seen: list[str] = []
    for atom, _ in sel.atoms:
        if atom.section == section and atom.group not in seen:
            seen.append(atom.group)
    return seen


def test_experience_prints_newest_first(dated):
    assert _section_order(dated, "experience") == [
        "exp_now", "exp_recent", "exp_term", "exp_old"]


def test_the_posting_does_not_reorder_experience(dated):
    """The whole point: `exp_old` matches the posting and still prints last."""
    order = _section_order(dated, "experience")
    assert order.index("exp_old") > order.index("exp_now")


def test_projects_still_print_best_match_first(dated):
    """Chronology is a convention about jobs, not a rule about everything."""
    assert _section_order(dated, "projects")[0] == "project_old"


def test_an_entry_with_no_date_does_not_lead(tmp_path):
    """Unparseable sorts to the bottom. A dateless entry is not the newest."""
    master = tmp_path / "undated.md"
    master.write_text(
        DATED.replace("dates: Aug 2026 -- Present", "dates: whenever"),
        encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    assert _section_order(Bank(tmp_path), "experience")[-1] == "exp_now"


@pytest.mark.parametrize("line, expect_newer_than", [
    ("Aug 2026 -- Present", "Aug 2026 -- Dec 2026"),
    ("Jan 2026 - Mar 2026", "Dec 2025"),
    ("2024 to 2025", "Jun 2024"),
    ("Fall 2025", "Jul 2025"),
    ("Spring 2026", "Fall 2025"),
    ("May 2026 – Present", "May 2026 -- Aug 2026"),
])
def test_a_date_line_is_read_the_way_it_is_written(line, expect_newer_than):
    """Dates are stored as the text that prints, in whatever form you wrote.

    Nothing may normalise the stored string -- the resume would start showing
    a date format you did not choose -- so the parser has to take the forms a
    master file actually contains.
    """
    assert date_key(line) > date_key(expect_newer_than), (
        f"{line!r} should sort above {expect_newer_than!r}")


def test_a_lone_hyphen_inside_a_word_is_not_a_range():
    # "-" only separates when spaced, or the parser would split names.
    assert date_key("Mid-2026") == date_key("2026")


# --------------------------------------------------------------------------- #
# how many bullets an entry gets is the loudest thing a resume says
# --------------------------------------------------------------------------- #
# Filling the page to the margin and printing Experience newest-first are both
# right, and together they put four bullets of a university help desk at the
# top of a research resume. Not because the posting wanted support work -- the
# desk scored 0.15 against a 0.72 top match -- but because four was the cap for
# everything and there was room left once the research was in.
SHARES = """## Experience

### Answers The Posting
id: exp_lead
dates: Jan 2026 -- May 2026
tags: python, kubernetes

- Ran Python services on Kubernetes, first bullet.
- Ran Python services on Kubernetes, second bullet.
- Ran Python services on Kubernetes, third bullet.
- Ran Python services on Kubernetes, fourth bullet.
- Ran Python services on Kubernetes, fifth bullet.

### Answers Nothing
id: exp_filler
dates: Aug 2026 -- Present
tags: curling, philately

- Swept the ice, first bullet.    [pin]
- Swept the ice, second bullet.
- Swept the ice, third bullet.
- Swept the ice, fourth bullet.
- Swept the ice, fifth bullet.
"""


@pytest.fixture
def shares(tmp_path) -> Bank:
    master = tmp_path / "shares.md"
    master.write_text(SHARES, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    return Bank(tmp_path)


def _counts(bank: Bank, jd: str = JD, budget: int = 60) -> dict[str, int]:
    out: dict[str, int] = {}
    for atom, _ in bank.select(jd, family="default", line_budget=budget).atoms:
        out[atom.group] = out.get(atom.group, 0) + 1
    return out


def test_a_supporting_entry_does_not_take_a_leading_entrys_space(shares):
    counts = _counts(shares)
    assert counts["exp_filler"] <= SUPPORTING_CAP, (
        "an entry the posting never asks for filled the page to the cap")
    assert counts["exp_lead"] > counts["exp_filler"]


def test_the_supporting_entry_still_appears(shares):
    # A smaller share, not no share. Dropping it would be a worse resume and
    # would also break the pin on its first bullet.
    assert _counts(shares)["exp_filler"] >= 1


def test_a_relevant_entry_keeps_the_full_cap(shares):
    assert _counts(shares)["exp_lead"] == 4


def test_the_cap_follows_the_posting_rather_than_the_entry(shares):
    """Same bank, a posting about curling: the shares swap."""
    counts = _counts(shares, "we are hiring for curling and philately")
    assert counts["exp_filler"] == 4
    assert counts["exp_lead"] <= SUPPORTING_CAP
