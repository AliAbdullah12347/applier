"""Defects found by rendering a real PDF rather than by reading the code.

Each of these was invisible to the existing suite and to inspection. They
surfaced only when a master resume written to the documented rules was pushed
all the way through import, lint, selection, pdflatex, and re-extraction.

That is the lesson worth keeping: every one of these is a *tokenisation* or
*error-path* bug, the kind that reads correctly and behaves wrongly.
"""

from __future__ import annotations

import re

import pytest

from applier.tailor.bank import BARE_NUMERAL_RE, Bank
from applier.tailor.master import build_bank

MASTER_HEAD = """## Skills

Languages: Python, TypeScript

## Experience

### Test Role
id: exp_test
dates: 2026
tags: python, testing

"""


def _bank(tmp_path, body: str) -> Bank:
    m = tmp_path / "master.md"
    m.write_text(MASTER_HEAD + body, encoding="utf-8")
    build_bank(m, tmp_path, write=True)
    return Bank(tmp_path)


# --------------------------------------------------------------------------- #
# the JD tokeniser
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("jd,token", [
    ("We use gRPC.", "grpc"),
    ("Experience with Kubernetes.", "kubernetes"),
    ("Our stack is Rust, Go and Kafka.", "kafka"),
    ("Strong Python.", "python"),
])
def test_a_technology_at_the_end_of_a_sentence_is_still_a_token(tmp_path, jd, token):
    """The dot is in the token class so "node.js" survives as one token — but
    it also swallowed a sentence-ending full stop, so "we use gRPC." tokenised
    to "grpc." and matched no tag. Any technology named at the end of a
    sentence was invisible to both relevance scoring and gap detection.
    """
    bank = _bank(tmp_path, "- A bullet about Python and testing\n")
    jd_tokens = {t.strip(".") for t in re.findall(r"[a-z][a-z0-9+#.]{1,}", jd.lower())}
    assert token in jd_tokens, f"{token!r} not tokenised out of {jd!r}"


def test_internal_dots_are_preserved(tmp_path):
    """Stripping the edges must not break the names the dot was there for."""
    jd = "We use Node.js, Three.js and Vue.js."
    tokens = {t.strip(".") for t in re.findall(r"[a-z][a-z0-9+#.]{1,}", jd.lower())}
    for name in ("node.js", "three.js", "vue.js"):
        assert name in tokens, f"{name!r} was damaged by edge-stripping"


def test_gaps_report_an_uncovered_technology(tmp_path):
    """The gap list is the whole 'what should I build next' signal."""
    bank = _bank(tmp_path, "- A bullet about Python and testing\n")
    sel = bank.select("We need Python plus Kubernetes, Kafka and Rust.",
                      family="default", line_budget=38)
    for expected in ("kubernetes", "kafka", "rust"):
        assert expected in sel.gaps, f"{expected!r} missing from gaps {sel.gaps}"
    assert "python" not in sel.gaps, "a covered skill must not be reported as a gap"


# --------------------------------------------------------------------------- #
# lint vs the importer: they must agree on what a numeral is
# --------------------------------------------------------------------------- #
def test_lint_and_importer_agree_on_trailing_punctuation(tmp_path):
    """A bullet ending in a small number made the bank unrenderable.

    The importer deliberately leaves bare 1-5 as literals (NUMERAL_SKIP), but
    lint's token kept the sentence's full stop, so "under 2." failed the same
    [1-5] exemption and lint demanded a claim the importer refuses to mint.
    There was no way to satisfy both except rewording the bullet.
    """
    bank = _bank(tmp_path, "- Cut a full rebuild from 14 minutes to under 2.\n")
    problems = [p for p in bank.lint() if "bare numeral" in p]
    assert not problems, f"lint demands a claim the importer will not create: {problems}"


@pytest.mark.parametrize("text", [
    "Shipped in under 2.",
    "Reduced the step count to 3.",
    "Supported 4, then 5.",
    "Finished in 2; shipped in 3.",
])
def test_small_counts_with_punctuation_do_not_fail_lint(tmp_path, text):
    bank = _bank(tmp_path, f"- {text}\n")
    assert not [p for p in bank.lint() if "bare numeral" in p], \
        f"{text!r} wrongly flagged"


# --------------------------------------------------------------------------- #
# the invariant that makes the above impossible, rather than merely absent
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bullet", [
    "Cut a full rebuild from 14 minutes to under 2.",
    "Built in Unreal Engine 5.3 with custom shaders",
    "Targeted Python 3.11 across the toolchain",
    "Shipped in 2026 after a 3-week push",
    "Built a 3D renderer and a 2D fallback",
    "Grew the team to 4, then to 5.",
    "Raised throughput 60-85% on a $4,000 budget",
    "Handled 12,400 requests a second at 95% utilisation",
    "Cut 8-arc classification from 3 days to 20 minutes (~216x)",
    "Reached 30 FPS, then 90 FPS, on consumer hardware",
])
def test_importer_output_always_passes_lint(tmp_path, bullet):
    """Whatever the importer produces must lint clean. Always.

    This is the invariant behind two separate bugs. The importer decides which
    numerals become claims; lint decides which numerals are allowed to remain
    literal. When those two disagree, the bank is unrenderable and there is no
    way for the user to fix it — they cannot register a claim the importer
    refuses to mint, so the only escape is rewording a perfectly good bullet.

    Both now go through `is_literal_numeral`, so a disagreement requires
    someone to bypass it — and this test is what notices.
    """
    bank = _bank(tmp_path, f"- {bullet}\n")
    problems = bank.lint()
    assert not problems, (
        f"the importer produced a bank that fails its own lint.\n"
        f"  bullet:  {bullet}\n"
        f"  problems: {problems}")


@pytest.mark.parametrize("phrasing,should_flag", [
    # Genuinely untracked measurements — must be caught.
    ("Grew the user base to 7500 accounts", True),
    ("Cut latency to 240 milliseconds", True),
    ("Handled 12,400 requests a second", True),
    ("Grew revenue by 40 percent", True),
    # Legitimately literal — must stay exempt, and the importer agrees.
    ("Shipped in 2026", False),               # a year
    ("Built a 3D renderer", False),           # digit glued to a letter
    ("Reduced the step count to 3.", False),  # trivial count, with punctuation
    ("Built in Unreal Engine 5.3", False),    # a version, not a measurement
    ("Targeted Python 3.11 throughout", False),
    ("Shipped on Unreal Engine 5", False),    # trivial count exemption covers it
])
def test_lint_still_catches_real_untracked_numbers(tmp_path, phrasing, should_flag):
    """The trailing-punctuation fix must not blunt the check it loosened.

    Written by hand into atoms.yaml rather than through the importer, because
    the importer would extract these into claims — the point is to prove lint
    objects when a phrasing carries a literal measurement.
    """
    import yaml

    _bank(tmp_path, "- A placeholder bullet about testing\n")
    atoms = tmp_path / "atoms.yaml"
    data = yaml.safe_load(atoms.read_text(encoding="utf-8"))
    data["atoms"][0]["phrasings"] = {"long": phrasing, "medium": phrasing,
                                     "short": phrasing}
    atoms.write_text(yaml.safe_dump(data), encoding="utf-8")

    problems = [p for p in Bank(tmp_path).lint() if "bare numeral" in p]
    if should_flag:
        assert problems, f"lint no longer catches the untracked numeral in {phrasing!r}"
    else:
        assert not problems, f"{phrasing!r} wrongly flagged: {problems}"


# --------------------------------------------------------------------------- #
# section headings: content must never vanish quietly
# --------------------------------------------------------------------------- #
ENTRY = ("### An Entry\nid: test_entry\ntags: python, testing\n\n"
         "- A bullet with enough content to be a real bullet\n"
         "  ~ A shorter version.\n  ~~ Short.\n")


@pytest.mark.parametrize("heading,expected", [
    ("Experience", "experience"),
    ("Work Experience", "experience"),
    ("Professional Experience", "experience"),
    ("Projects", "projects"),
    ("Selected Projects", "projects"),
    ("Publications", "projects"),
    ("Research", "projects"),
    ("Leadership", "leadership"),
    ("Activities", "leadership"),
    ("Education", "education"),
    ("Honors", "education"),
    ("Honors & Awards", "education"),
    ("Awards and Honors", "education"),
])
def test_plausible_section_headings_all_map_to_a_rendered_section(
        tmp_path, heading, expected):
    """A heading that maps nowhere is content that imports, lints and then
    never reaches a page. `## Honors & Awards` and `## Publications` are both
    things a writer reaches for naturally, and both used to vanish."""
    m = tmp_path / "master.md"
    m.write_text(f"## {heading}\n\n{ENTRY}", encoding="utf-8")
    build_bank(m, tmp_path, write=True)
    bank = Bank(tmp_path)
    assert bank.atoms, f"'## {heading}' produced no atoms at all"
    assert bank.atoms[0].section == expected, (
        f"'## {heading}' mapped to {bank.atoms[0].section!r}, not {expected!r}")
    assert not bank.lint(), f"'## {heading}' lints dirty: {bank.lint()}"


@pytest.mark.parametrize("heading", ["Technical Skills", "Skills", "Skills & Tools"])
def test_skills_heading_aliases(tmp_path, heading):
    m = tmp_path / "master.md"
    m.write_text(f"## {heading}\n\nLanguages: Python, C++\n", encoding="utf-8")
    build_bank(m, tmp_path, write=True)
    assert Bank(tmp_path).skills.get("Languages") == ["Python", "C++"]


def test_an_unrecognised_heading_fails_lint_rather_than_vanishing(tmp_path):
    """The safety net. An alias list can never be complete, so the remaining
    case must be loud."""
    m = tmp_path / "master.md"
    m.write_text(f"## Miscellaneous Ephemera\n\n{ENTRY}", encoding="utf-8")
    build_bank(m, tmp_path, write=True)
    problems = Bank(tmp_path).lint()
    assert problems, "content in an unknown section passed lint and will never render"
    assert "never appear" in problems[0], f"the message does not explain the risk: {problems[0]}"


def test_a_file_copied_from_rendered_markdown_is_refused(tmp_path):
    """The exact failure that reached a real user.

    Copying the master resume out of a chat's *rendered* view rather than its
    code block strips every `#` marker and folds the metadata lines into one
    paragraph. The result parsed to zero entries, imported "successfully",
    and then linted clean — vacuously, because an empty bank has nothing to
    find fault with. You would believe it had worked until a render came back
    empty, which might be days later.
    """
    from applier.tailor.master import MasterError

    flattened = (
        "Skills\n"
        "Languages: Python, TypeScript\n"
        "Experience\n"
        "Student Consultant — Colgate ITS\n"
        "id: exp_its org: Service Desk dates: Fall 2026 -- Present tags: support\n"
        "* Provide frontline support ~ Provide support. ~~ Support.\n"
    )
    m = tmp_path / "master.md"
    m.write_text(flattened, encoding="utf-8")

    with pytest.raises(MasterError) as err:
        build_bank(m, tmp_path, write=True)
    msg = str(err.value)
    assert "no entries" in msg
    assert "code block" in msg, "the message must say how to fix it"


def test_a_skills_only_file_is_still_valid(tmp_path):
    """Zero entries is legitimate when the file is only a skills block."""
    m = tmp_path / "master.md"
    m.write_text("## Skills\n\nLanguages: Python, C++\n", encoding="utf-8")
    build_bank(m, tmp_path, write=True)          # must not raise
    assert Bank(tmp_path).skills.get("Languages") == ["Python", "C++"]


def test_an_empty_bank_does_not_lint_clean(tmp_path):
    """"0 problems" on a bank that can render nothing is not a pass."""
    (tmp_path / "atoms.yaml").write_text("atoms: []\n", encoding="utf-8")
    problems = Bank(tmp_path).lint()
    assert problems, "an empty bank reported no problems"
    assert "empty" in problems[0].lower()


def test_every_renderable_section_is_reachable_by_some_heading():
    """A section the template renders but no heading maps to would be dead."""
    from applier.tailor.master import KNOWN_SECTIONS, RENDERABLE_SECTIONS
    reachable = set(KNOWN_SECTIONS.values())
    unreachable = RENDERABLE_SECTIONS - reachable
    assert not unreachable, f"no heading maps to: {sorted(unreachable)}"


# --------------------------------------------------------------------------- #
# verification: warnings must stay meaningful
# --------------------------------------------------------------------------- #
def test_profile_numerals_are_not_reported_as_untraced():
    """GPA and the graduation date are typeset from the profile, not the bank,
    so they have no claim to trace to. Flagging them put a permanent warning
    on every resume ("numeral '00' not traced", from GPA 3.98/4.00) — and a
    check that always warns is a check people stop reading."""
    import inspect

    from applier.tailor import render
    sig = inspect.signature(render.verify_pdf)
    assert "profile_numerals" in sig.parameters, (
        "verify_pdf cannot be told about profile-sourced figures")

    src = inspect.getsource(render.verify_pdf)
    assert "profile_numerals or []" in src, (
        "profile_numerals is accepted but never added to the allowed set")


# --------------------------------------------------------------------------- #
# the error path
# --------------------------------------------------------------------------- #
def test_a_non_length_verification_failure_reports_its_reason():
    """`build_resume` broke out of the autofit loop before recording why.

    A non-length failure therefore raised "failed verification after 5
    attempts: None" and discarded the diagnosis — the one thing needed to fix
    it. Checked by source order because reproducing it needs a full pdflatex
    render of a deliberately broken bank.
    """
    import inspect

    from applier.tailor import pipeline
    src = inspect.getsource(pipeline.build_resume)

    assign = src.index("last_err = TailorError(str(report))")
    guard = src.index("if pages_ok:")
    assert assign < guard, (
        "last_err is assigned after the `if pages_ok: break`, so a non-length "
        "failure still raises with `None` as its reason")


def test_the_latex_escape_table_covers_every_special_character():
    """Proven to reach a rendered PDF intact: & % # $ _ ^ { } ~ and the
    composite forms C#, F#, R&D, snake_case, 60-85%, $12,500."""
    from applier.tailor.render import LATEX_SPECIAL, tex_escape

    for ch in "&%#$_^{}~\\":
        assert ch in LATEX_SPECIAL, f"{ch!r} is not escaped"

    out = tex_escape("C# and F# with snake_case, 60-85% of $12,500 ~ R&D {x}")
    for raw in ("\\#", "\\_", "\\%", "\\$", "\\&", "\\{", "\\}", "textasciitilde"):
        assert raw in out, f"{raw} missing from {out!r}"
