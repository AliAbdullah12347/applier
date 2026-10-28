"""What a lint finding costs, and what it is allowed to stop.

`build_resume` refused to render on any lint finding at all. The rule it was
protecting is a real one -- a number the ledger cannot trace must never reach
a page -- but a style rule had been added beside it, and style rules are not
that. A master resume with twenty-five sentences too long for the column
therefore presented as a broken installation: preflight red on two counts,
Apply refusing to start, and the remedy on offer was to go and shorten
bullets before any resume could be produced at all.

Refusing to render is the loudest thing this program does. It is reserved for
findings that would make the page wrong.
"""

from __future__ import annotations

import pytest

from applier.tailor.bank import ADVISORY, BLOCKING, Bank
from applier.tailor.master import build_bank

LONG = ("Maintained the production deployment pipeline across every supported "
        "configuration, environment and target platform, coordinating releases "
        "with four separate teams on a weekly cadence throughout the year")

MASTER = f"""## Skills

Languages: Python

## Experience

### A Role
id: exp_role
dates: 2026
tags: python

- {LONG}
  ~ {LONG}
  ~~ Short.
- Wrote the deployment tooling in Python.
"""


@pytest.fixture
def wordy(tmp_path) -> Bank:
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    return Bank(tmp_path)


def test_a_bullet_too_long_for_the_column_is_reported(wordy):
    assert any("no phrasing fits one line" in p for p in wordy.lint())


def test_a_bullet_too_long_for_the_column_does_not_block_rendering(wordy):
    """The whole point. It is a worse page, not a wrong one."""
    assert wordy.blocking_problems() == []


def test_a_number_the_ledger_cannot_trace_still_blocks(tmp_path):
    """The rule the gate exists for, unchanged."""
    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    bank = Bank(tmp_path)
    # Retiring a claim is a deliberate "this number must never render again".
    for section in bank.claims.values():
        if isinstance(section, dict):
            for claim in section.values():
                if isinstance(claim, dict):
                    claim["status"] = "RETIRED"
    bank._len_cache.clear()
    atom = bank.atoms[0]
    atom.phrasings["medium"] = "Cut the job to {{C.made.up}} of what it was."
    assert any("made.up" in p for p in bank.blocking_problems())


def test_an_empty_bank_blocks(tmp_path):
    (tmp_path / "atoms.yaml").write_text("atoms: []\n", encoding="utf-8")
    assert Bank(tmp_path).blocking_problems(), "a bank that renders nothing is not fine"


def test_every_finding_carries_a_severity(wordy):
    assert all(sev in (BLOCKING, ADVISORY) for sev, _ in wordy.findings())


def test_lint_is_still_everything(wordy):
    assert sorted(wordy.lint()) == sorted(
        wordy.blocking_problems() + wordy.advisory_problems())


def test_blocking_findings_are_listed_first(tmp_path):
    """`bank lint` prints the list in order; the one that stops a render leads."""
    master = tmp_path / "master.md"
    master.write_text(MASTER + "\n- Shipped 7 releases to 4300 users.\n",
                      encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    bank = Bank(tmp_path)
    bank.claims.clear()                       # every slot now unresolvable
    bank._len_cache.clear()
    problems, blocking = bank.lint(), set(bank.blocking_problems())
    assert blocking, "expected at least one blocking finding to order"
    first_advisory = next((i for i, p in enumerate(problems) if p not in blocking),
                          len(problems))
    last_blocking = max(i for i, p in enumerate(problems) if p in blocking)
    assert last_blocking < first_advisory


class _PastTheGate(Exception):
    """Raised by the first step after the gate, to prove the gate let us by."""


def test_build_resume_does_not_refuse_over_a_long_bullet(tmp_path, monkeypatch):
    """The gate is what the user actually hits, so test it there.

    Rendering a real PDF to prove this would take pdflatex and a profile; the
    question is only whether `build_resume` gets past its own first check, so
    the step right after it raises and the test asks which exception arrives.
    """
    from applier.tailor import pipeline

    master = tmp_path / "master.md"
    master.write_text(MASTER, encoding="utf-8")
    build_bank(master, tmp_path, write=True)
    assert Bank(tmp_path).lint(), "this bank is supposed to have findings"

    monkeypatch.setattr(pipeline, "job_family",
                        lambda job: (_ for _ in ()).throw(_PastTheGate()))
    settings = type("S", (), {"get": lambda self, k, d=None: str(tmp_path)
                              if k == "tailor.bank_path" else d})()
    with pytest.raises(_PastTheGate):
        pipeline.build_resume({"title": "x", "description": "python"},
                              settings, {}, tmp_path)
