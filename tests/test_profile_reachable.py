"""Every field the app says is missing must be fillable in the app.

The Profile tab told somebody "24 fields across the profile still say ASK"
after they had filled in every field it offered. Those 24 were exactly the
ones with no input anywhere: `unanswered()` walks the whole profile, the panel
renders only what `PROFILE_WRITABLE` lists, and the difference was unreachable
by any means short of editing YAML by hand.

It was not only confusing. Preflight blocks on
`work_authorization.answers.non_us_qualifier`, which was in that gap -- a
required answer with nowhere to type it, on an install that therefore could
never pass its own readiness check.

So the rule is an invariant: the set of things that can be reported missing
and the set of things that can be set are the same set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from applier.config import Config, unanswered
from applier.web.api import PROFILE_WRITABLE

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "config" / "profile.example.yaml"


@pytest.fixture(scope="module")
def example() -> Config:
    return Config(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))


def test_nothing_can_be_reported_missing_without_being_fillable(example):
    gaps = unanswered(example)
    assert gaps, "the example profile has no ASK fields; this test is testing nothing"
    unreachable = sorted(g for g in gaps if g not in PROFILE_WRITABLE)
    assert not unreachable, (
        "these are reported as unanswered but cannot be set from the app, so "
        "the count can never reach zero:\n  " + "\n  ".join(unreachable))


def test_the_answer_preflight_blocks_on_is_one_of_them(example):
    """The specific field that made an install unable to pass its own check."""
    assert "work_authorization.answers.non_us_qualifier" in PROFILE_WRITABLE


def test_every_writable_path_can_actually_be_written():
    """A path in the allow-list that Config.set cannot address is worse than
    one that is missing: the save reports success and changes nothing."""
    cfg = Config({})
    for path in sorted(PROFILE_WRITABLE):
        cfg.set(path, "value")
    for path in sorted(PROFILE_WRITABLE):
        node = cfg.raw
        import re as _re
        for seg in path.split("."):
            m = _re.match(r"^(.*?)\[(\d+)\]$", seg)
            if m:
                node = node[m.group(1)][int(m.group(2))]
            else:
                node = node[seg]
        assert node == "value", f"{path} did not round-trip through Config.set"


def test_a_filled_profile_reports_nothing_missing(example):
    """The end state a user is entitled to reach."""
    cfg = Config(yaml.safe_load(EXAMPLE.read_text(encoding="utf-8")))
    for path in unanswered(cfg):
        cfg.set(path, "answered")
    assert unanswered(cfg) == []


@pytest.mark.parametrize("path", [
    "education[0].institution",
    "education[0].expected_graduation",
    "references[0].email",
    "languages[1].level",
    "links.portfolio",
    "identity.preferred_name",
])
def test_the_fields_that_were_stranded_are_listed(path):
    assert path in PROFILE_WRITABLE


def test_the_panel_can_read_an_indexed_path_back():
    """dig() renders the current value; without index support the input shows
    empty and saving an untouched field looks like an edit."""
    src = (ROOT / "applier" / "web" / "static" / "panels" / "settings.js").read_text(
        encoding="utf-8")
    dig = src[src.index("function dig("):src.index("function dig(") + 600]
    assert "[" in dig and "Array.isArray" in dig, (
        "dig() no longer understands list indices")
