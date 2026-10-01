"""Regression tests for claim identity and binding in the master-resume importer.

Three separate bugs here bound one bullet's prose to another bullet's number,
and each was invisible in the YAML — you only saw it in the rendered sentence:

  1. digits-only identity:  a bare "70" from "70-200 attendees" bound to an
     unrelated "70%", so editing the attendance range silently changed a
     percentage in a different role.
  2. digits+suffix identity: "30 students" bound to "30 FPS".
  3. sequential names: the claim counter reset per bullet, so two bullets in the
     same entry both minted "n1" and the second overwrote the first. That one
     rendered as "reducing manual modeling time by 200+ students".
  4. discarded variant claims: a "~" variant minted claims the caller threw
     away, leaving the variant pointing at a slot that did not exist.

They share a root: a number's identity is its digits AND its unit, and a claim's
name must be derived from its value rather than its position. A claim ledger
whose entries silently cross-bind is worse than no ledger, because it looks
authoritative while being wrong.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from applier.tailor.master import _claim_name, _shape, build_bank, parse


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("a,b", [
    ("30 FPS", "30 students"),
    ("70%", "70-200 attendees"),
    ("60 FPS", "60+ tickets"),
    ("200+ students", "200 attendees"),
    ("1,000+ applicants", "1000 users"),
])
def test_different_units_are_different_facts(a, b):
    assert _shape(a) != _shape(b), f"{a!r} and {b!r} collapse to the same claim"


@pytest.mark.parametrize("a,b", [
    ("30 FPS", "30 fps"),
    ("$5,000", "5000"),
    ("~216x", "216x"),
])
def test_same_fact_written_differently_is_one_claim(a, b):
    assert _shape(a) == _shape(b)


def test_claim_names_are_value_derived_not_positional():
    """Two different values in one entry must never get the same name."""
    assert _claim_name("70%") != _claim_name("200+ students")
    assert _claim_name("70%") == _claim_name("70%")
    assert _claim_name("70%").startswith("n_")


@pytest.mark.parametrize("a,b", [
    ("8", "8!"),            # the one that shipped: "8-arc" rendered as "8!-arc"
    ("25", "25+"),
    ("70", "70%"),
    ("3", "3x"),
    ("5", "5.0"),
    ("10", "10-20"),
    ("4", "4%"),
    ("6", "6!"),
])
def test_distinct_values_never_share_a_claim_name(a, b):
    """A name collision silently binds two unrelated facts to one claim.

    `_shape` already told these apart, but `_claim_name` stripped the suffix,
    so "8" and "8!" both became `n_8`. The second minted claim overwrote the
    first, and a bullet reading "Cut 8-arc classification runtime ... across
    each candidate's 8! point-permutations" rendered on a real PDF as
    "Cut 8!-arc classification runtime". Found by rendering, not by reading.
    """
    assert _claim_name(a) != _claim_name(b), (
        f"{a!r} and {b!r} share the claim name {_claim_name(a)!r}; one will "
        f"overwrite the other and both slots will render the same value")


def test_name_collision_implies_identity_collision():
    """The invariant that makes the above impossible by construction.

    Two values may share a name only if they are the same fact. Checked over a
    spread of forms rather than a handful of pairs, so adding a new suffix to
    NUMERAL_RE without teaching `_claim_name` about it fails here.
    """
    values = [
        "8", "8!", "25", "25+", "70", "70%", "3", "3x", "5.0", "10-20",
        "216x", "~216x", "$4,000", "4000", "30 FPS", "30 students",
        "1,000+ applicants", "12,344", "60-85%", "2 hours", "2 minutes",
    ]
    by_name: dict[str, list[str]] = {}
    for v in values:
        by_name.setdefault(_claim_name(v), []).append(v)

    for name, group in by_name.items():
        shapes = {_shape(v) for v in group}
        assert len(shapes) == 1, (
            f"claim name {name!r} is shared by values with different "
            f"identities: {group} -> shapes {shapes}")


# --------------------------------------------------------------------------- #
# end-to-end import
# --------------------------------------------------------------------------- #
MASTER = textwrap.dedent("""
    ## Skills
    Languages: Python

    ## Experience

    ### Example Role
    id: exp_demo
    tags: python
    dates: 2025

    - Served 200+ students across 4 university courses
    - Reduced manual work by 70%
      ~ Cut manual work 70%.
    - Sustained 60 FPS on the display wall
    - Selected as 1 of 30 students nationwide
""").strip()


@pytest.fixture
def built(tmp_path):
    (tmp_path / "master_resume.md").write_text(MASTER, encoding="utf-8")
    build_bank(tmp_path / "master_resume.md", tmp_path)
    atoms = yaml.safe_load((tmp_path / "atoms.yaml").read_text(encoding="utf-8"))
    claims = yaml.safe_load((tmp_path / "claims.yaml").read_text(encoding="utf-8"))
    return atoms, claims


def _rendered(atoms, claims, needle, size="long"):
    """Resolve slots the way the renderer does, for one bullet."""
    import re
    for a in atoms["atoms"]:
        text = a["phrasings"][size]
        flat = re.sub(r"\{\{C\.[^}]+\}\}", "", text)
        if needle in flat:
            def sub(m):
                sect, name = m.group(1).split(".", 1)
                return str(claims[sect][name]["value"])
            return re.sub(r"\{\{C\.([^}]+)\}\}", sub, text)
    raise AssertionError(f"no bullet containing {needle!r}")


def test_each_bullet_keeps_its_own_number(built):
    atoms, claims = built
    assert "200+ students" in _rendered(atoms, claims, "across")
    assert "70%" in _rendered(atoms, claims, "Reduced manual work")
    assert "60 FPS" in _rendered(atoms, claims, "Sustained")
    assert "30 students" in _rendered(atoms, claims, "Selected as")


def test_no_cross_contamination(built):
    """The exact failure that shipped: one bullet showing another's number."""
    atoms, claims = built
    pct = _rendered(atoms, claims, "Reduced manual work")
    assert "students" not in pct, f"70% bullet contaminated: {pct!r}"
    fps = _rendered(atoms, claims, "Sustained")
    assert "students" not in fps, f"FPS bullet contaminated: {fps!r}"


def test_variant_phrasings_have_no_dangling_slots(built):
    """A '~' variant must mint and keep its own claims."""
    atoms, claims = built
    for a in atoms["atoms"]:
        for size, text in a["phrasings"].items():
            import re
            for key in re.findall(r"\{\{C\.([^}]+)\}\}", text):
                sect, name = key.split(".", 1)
                assert sect in claims and name in claims[sect], (
                    f"{a['id']}.{size} references missing claim {key}")


def test_retired_claims_are_not_resurrected_by_reimport(tmp_path):
    """The point of the ledger: a withdrawn number cannot come back."""
    (tmp_path / "master_resume.md").write_text(MASTER, encoding="utf-8")
    build_bank(tmp_path / "master_resume.md", tmp_path)

    claims = yaml.safe_load((tmp_path / "claims.yaml").read_text(encoding="utf-8"))
    target = None
    for sect, cl in claims.items():
        for name, cm in cl.items():
            if str(cm.get("value")).strip() == "70%":
                target = (sect, name)
    assert target, "70% claim not found"
    claims[target[0]][target[1]]["status"] = "RETIRED"
    (tmp_path / "claims.yaml").write_text(
        yaml.safe_dump(claims, sort_keys=False), encoding="utf-8")

    build_bank(tmp_path / "master_resume.md", tmp_path)
    after = yaml.safe_load((tmp_path / "claims.yaml").read_text(encoding="utf-8"))
    assert after[target[0]][target[1]]["status"] == "RETIRED"

    atoms = yaml.safe_load((tmp_path / "atoms.yaml").read_text(encoding="utf-8"))
    for a in atoms["atoms"]:
        for text in a["phrasings"].values():
            assert "70%" not in text, "a retired number reappeared as literal text"


def test_version_numbers_are_not_claims(tmp_path):
    src = textwrap.dedent("""
        ## Projects
        ### Demo
        id: p
        tags: graphics
        - Built it in Unreal Engine 5.3 with custom shaders
    """).strip()
    (tmp_path / "master_resume.md").write_text(src, encoding="utf-8")
    build_bank(tmp_path / "master_resume.md", tmp_path)
    atoms = yaml.safe_load((tmp_path / "atoms.yaml").read_text(encoding="utf-8"))
    assert "Unreal Engine 5.3" in atoms["atoms"][0]["phrasings"]["long"]


# --------------------------------------------------------------------------- #
def test_pins_are_parsed(tmp_path):
    src = textwrap.dedent("""
        ## Experience
        ### Pinned Role    [pin]
        id: r
        tags: python
        - always here
        ### Normal Role
        id: r2
        tags: python
        - sometimes here    [pin]
        - not pinned
    """).strip()
    (tmp_path / "master_resume.md").write_text(src, encoding="utf-8")
    entries, _ = parse(tmp_path / "master_resume.md")
    assert entries[0].pinned
    assert not entries[1].pinned
    assert entries[1].bullets[0]["pinned"]
    assert not entries[1].bullets[1]["pinned"]
