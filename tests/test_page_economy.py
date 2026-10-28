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

from applier.tailor.bank import SHORTEN_KEEPS, WRAP_COLUMN, Bank
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
