"""The content bank: atoms, claims, selection.

The central design decision of the whole system: **the engine selects and orders
pre-authored phrasings. It never generates resume prose.**

Generation is where truthfulness dies. A model asked to "tailor this bullet to
the job" will quietly upgrade "contributed to" into "led", invent a percentage
that reads well, or merge two roles into one. Selection cannot do any of that,
because every candidate string was written by you in advance.

Numbers are stricter still. Phrasings carry `{{C.path}}` slots resolved from
claims.yaml at render time. A phrasing containing a bare numeral fails lint. A
claim marked RETIRED never resolves. So a retired metric cannot reappear by
accident, no matter what the matcher thinks the job wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .master import RENDERABLE_SECTIONS, is_literal_numeral

SLOT_RE = re.compile(r"\{\{\s*C\.([A-Za-z0-9_.]+)\s*\}\}")
# Bare numerals that must come from claims instead. Ignores things like "8-arc"
# that are part of a name, and ordinals inside words.
BARE_NUMERAL_RE = re.compile(r"(?<![\w.\-])\d[\d,.]*\s*(?:%|x\b|\+)?")

# Tags beyond this count no longer dilute an atom's relevance. Eight is plenty
# to say what a bullet is about; past that the author is being thorough rather
# than vague, and should not be scored down for it.
TAG_DENOM_CAP = 8

# Bonus for an entry declaring itself the lead for this job family.
LEAD_FOR_BONUS = 0.45


class BankError(RuntimeError):
    pass


@dataclass
class Atom:
    id: str
    section: str
    group: str
    tags: list[str] = field(default_factory=list)
    lead_for: list[str] = field(default_factory=list)
    phrasings: dict[str, str] = field(default_factory=dict)
    pinned: bool = False          # non-negotiable: always appears

    def text(self, size: str = "medium") -> str:
        return self.phrasings.get(size) or self.phrasings.get("medium") or ""


# How many bullets an entry must have on the page before its tags may vouch
# for a skill none of those bullets mentions.
TAG_VOUCH_MIN_BULLETS = 3

# Characters that fit on one line of the body column, including the bullet
# glyph and its space. Everything that reasons about how much page a bullet
# costs goes through this, so the selector and the quality harness cannot
# drift apart about what "wraps" means.
#
# Measured, not guessed. Across 140 rendered bullets the body column takes
# between 118 and 133 characters depending on the glyph mix, so this sits
# just under the narrowest real line. It was 105 on the first pass, which
# cost about an eighth of every bullet to a wrap that was never going to
# happen. To re-measure after a template or font change, run
# `scripts/check_families.py` and compare each bullet's rendered width
# against the right text edge.
WRAP_COLUMN = 112

# How much of the authored sentence a shorter phrasing has to keep before
# dropping to it is worth what it loses. At 0.7 a 112-character bullet gives
# up its second line for an 85-character one, but not for a 30-character
# stub: the point is to stop bullets wrapping, not to strip the page bare.
SHORTEN_KEEPS = 0.7

_SKILL_STOPWORDS = {"and", "or", "of", "the", "for", "with", "in", "on", "a"}


def _all_words_tagged(skill: str, tags: str) -> bool:
    """Is every content word of this skill present in the entry's tags?

    Tags are single words -- `troubleshooting`, `ticketing` -- while a skill
    may be a phrase: "hardware and software troubleshooting". Matching the
    phrase against the tag blob fails, and matching any single word would let
    a `python` tag vouch for "python bytecode internals". Requiring all of
    them is the reading that holds in both directions.
    """
    words = [w for w in re.findall(r"[a-z0-9+#]+", (skill or "").lower())
             if w not in _SKILL_STOPWORDS]
    if not words:
        return False
    return all(re.search(rf"(?<![\w#+]){re.escape(w)}(?![\w#+])", tags) for w in words)


def mentions(haystack: str, skill: str) -> bool:
    """Does this text actually use that skill?

    Short and punctuated names need a real boundary: plain substring matching
    finds "C" inside "Linux", "R" inside "React", and "Go" inside "Django".
    Longer alphabetic names are matched as substrings so that "real-time
    rendering" is found inside "real-time rendering pipelines".
    """
    s = (skill or "").lower().strip()
    if not s:
        return False
    if len(s) <= 4 or not s.replace(" ", "").isalpha():
        return re.search(rf"(?<![\w#+.]){re.escape(s)}(?![\w#+.])", haystack) is not None
    return s in haystack


@dataclass
class Selection:
    atoms: list[tuple[Atom, str]]          # (atom, size)
    score: float
    gaps: list[str]                        # JD keywords with no matching atom
    claims_used: dict[str, str]
    dropped: int = 0                       # atoms that did not fit
    pinned_dropped: list[str] = field(default_factory=list)


class Bank:
    def __init__(self, bank_dir: Path) -> None:
        self.dir = Path(bank_dir)
        self.atoms: list[Atom] = []
        self.sections: dict[str, dict] = {}
        self.skills: dict[str, list[str]] = {}
        self.roles: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self._len_cache: dict[tuple[str, str], int] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        apath, cpath = self.dir / "atoms.yaml", self.dir / "claims.yaml"
        if not apath.exists():
            raise BankError(f"No atom bank at {apath}. Copy atoms.example.yaml and edit it.")
        adata = yaml.safe_load(apath.read_text(encoding="utf-8")) or {}
        self.sections = adata.get("sections", {})
        self.skills = adata.get("skills", {})
        self.roles = adata.get("roles", {})
        for raw in adata.get("atoms", []):
            self.atoms.append(Atom(
                id=raw["id"], section=raw.get("section", "experience"),
                group=raw.get("group", raw["id"]), tags=raw.get("tags", []) or [],
                lead_for=raw.get("lead_for", []) or [],
                phrasings=raw.get("phrasings", {}) or {},
                pinned=bool(raw.get("pinned", False)),
            ))
        if cpath.exists():
            self.claims = yaml.safe_load(cpath.read_text(encoding="utf-8")) or {}

    # ------------------------------------------------------------------ #
    def claim(self, dotted: str) -> tuple[str | None, str]:
        """Resolve a claim slot. Returns (value, status). RETIRED never renders."""
        node: Any = self.claims
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return None, "missing"
            node = node[part]
        if not isinstance(node, dict):
            return None, "malformed"
        status = str(node.get("status", "needs_check"))
        if status == "RETIRED":
            return None, "RETIRED"
        return str(node.get("value", "")), status

    def resolve(self, text: str, *, strict: bool = True) -> tuple[str, dict[str, str]]:
        """Substitute {{C.x.y}} slots. Raises if a slot is retired or missing."""
        used: dict[str, str] = {}

        def sub(m: re.Match) -> str:
            key = m.group(1)
            val, status = self.claim(key)
            if val is None:
                if strict:
                    raise BankError(
                        f"claim '{key}' is {status} — phrasing cannot render. "
                        f"Either restore the claim or remove the phrasing."
                    )
                return ""
            used[key] = val
            return val

        return SLOT_RE.sub(sub, text), used

    def renderable(self, atom: Atom, size: str) -> bool:
        """False when any slot in this phrasing is retired or missing."""
        try:
            self.resolve(atom.text(size), strict=True)
            return True
        except BankError:
            return False

    # ------------------------------------------------------------------ #
    def lint(self) -> list[str]:
        """Structural problems that would let an untrue claim through."""
        problems: list[str] = []

        # An empty bank has nothing to find fault with, so it used to lint
        # clean — the most misleading possible result. "0 problems" on a bank
        # that can render nothing is not a pass.
        if not self.atoms:
            problems.append(
                "the bank is empty: no atoms were loaded from "
                f"{self.dir / 'atoms.yaml'}. Run `bank import` and check it "
                "reported a non-zero entry count.")
            return problems
        # An atom in a section no template renders is content that imported
        # cleanly, linted cleanly, and will never reach a page. Reporting it is
        # the difference between a typo you fix in ten seconds and a bullet you
        # believe is on your resume for a month.
        for a in self.atoms:
            if a.section not in RENDERABLE_SECTIONS:
                problems.append(
                    f"{a.id}: section {a.section!r} is not rendered by any resume "
                    f"template, so this bullet can never appear. Use one of: "
                    f"{', '.join(sorted(RENDERABLE_SECTIONS))}")
            if not a.phrasings:
                problems.append(f"{a.id}: no phrasings")
            # The only remaining source of a two-line bullet. The selector
            # takes the fullest phrasing that fits the column, so an atom
            # whose sentence overruns it and whose stub is too thin to stand
            # in wraps on every resume it reaches. That is an authoring
            # defect, and it is invisible until you look at a rendered page.
            elif a.section in RENDERABLE_SECTIONS:
                fits = [z for z in ("long", "medium", "short")
                        if self.renderable(a, z) and self._lines(a, z) == 1]
                keeps = (self._rendered_len(a, fits[0]) / self._reference_len(a)
                         if fits and self._reference_len(a) else 0.0)
                if not fits or keeps < SHORTEN_KEEPS:
                    width = self._rendered_len(a, "medium")
                    problems.append(
                        f"{a.id}: no phrasing fits one line -- the sentence "
                        f"renders {width} characters against a {WRAP_COLUMN}-"
                        f"character column, and the short form keeps only "
                        f"{keeps:.0%} of it. This bullet wraps on every resume "
                        f"it appears on. Tighten the `~` phrasing to "
                        f"{WRAP_COLUMN} characters or fewer.")
            for size, text in a.phrasings.items():
                stripped = SLOT_RE.sub("", text)
                for m in BARE_NUMERAL_RE.finditer(stripped):
                    # `[\d,.]*` swallows a sentence-ending full stop, so
                    # "...to under 2." yielded the token "2." — which then
                    # failed the [1-5] skip below that the importer applies to
                    # "2", and lint demanded a claim the importer deliberately
                    # refuses to mint. The bank became unrenderable with no
                    # remedy but rewording. Trailing punctuation is not part of
                    # the number.
                    tok = m.group(0).strip().rstrip(".,;:")
                    if not tok:
                        continue
                    # These are not measurements and must stay literal, or the
                    # ledger fills with noise and the real check gets ignored:
                    #   years (2026), trivial counts (1-5, as in "3D", "one of 4"),
                    #   and digits glued to a letter (3D, v5.3, C4).
                    # The "glued to a letter" exemption is for 3D, C4, v5.3 —
                    # a digit that is part of a name. But the pattern also
                    # consumes trailing whitespace, so `m.end()` landed past
                    # the space and "7500 accounts" looked glued too. That
                    # exempted almost every real measurement, which is the
                    # exact thing this check exists to catch. The tail must be
                    # the character immediately after the numeral.
                    end = m.start() + len(m.group(0).rstrip())
                    tail = stripped[end:end + 1]
                    # Years, trivial counts and version numbers -- the SAME
                    # rule the importer applies when deciding not to mint a
                    # claim. Keeping them in one function is what stops lint
                    # demanding a claim the importer refuses to create.
                    if is_literal_numeral(tok, stripped[max(0, m.start() - 24):m.start()]):
                        continue
                    if tail.isalpha():
                        continue        # digits glued to a letter: 3D, C4
                    problems.append(
                        f"{a.id}.{size}: bare numeral {tok!r} — move it into claims.yaml")
                try:
                    self.resolve(text, strict=True)
                except BankError as e:
                    problems.append(f"{a.id}.{size}: {e}")
        return problems

    # ------------------------------------------------------------------ #
    def relevance(self, atom: Atom, jd: str, jd_tokens: set[str], family: str) -> float:
        hits = sum(1 for t in atom.tags if t.replace("_", " ") in jd or t in jd_tokens)

        # Dividing by the full tag count punished thorough tagging: an atom with
        # 12 well-chosen tags and 3 hits scored 0.25, losing to one with 3 tags
        # and 2 hits at 0.67. That is backwards, and it contradicted the advice
        # to tag generously -- the better-described entry ranked lower for the
        # same evidence.
        #
        # The denominator is meant to express "how specific is this atom", not
        # "how many words did the author type", so it is capped. Past the cap,
        # extra tags are free: they can win matches but can no longer dilute.
        denom = max(2.0, float(min(len(atom.tags) or 1, TAG_DENOM_CAP)))
        rel = hits / denom
        if family in atom.lead_for:
            # A thumb on the scale, not the whole scale. At 0.75 this term
            # swamped everything: an entry hitting "agents" and "react" on a
            # job asking for agents and React scored 0.25, while an unrelated
            # entry carrying lead_for scored 1.40 and took four slots. The
            # declared lead should break ties between comparable entries, not
            # overrule what the posting actually asks for.
            rel += LEAD_FOR_BONUS
        if atom.section == "experience":
            rel += 0.15
        return rel

    def _reference_len(self, atom: Atom) -> int:
        """The width of the phrasing this bullet is *meant* to be.

        `medium` is the authoring default -- one sentence, one line -- so it
        is what "did shortening gut this bullet" is measured against. Using
        the longest phrasing instead let one bloated `long` drag the ratio
        and talked the selector out of shortening anything.
        """
        for size in ("medium", "long", "short"):
            if self.renderable(atom, size):
                return self._rendered_len(atom, size)
        return 0

    def _best_size(self, atom: Atom) -> str:
        """Which phrasing to print. A question about lines, not importance.

        Sizing by relevance put the strongest evidence in the longest
        phrasing, so the better a resume matched the worse it read: a
        real-time graphics posting came back with thirteen of its sixteen
        bullets running to two lines, and a recruiter skims a wrapped bullet
        rather than reading it.

        Below the column width an extra clause is free -- a fifty-character
        bullet and a hundred-character one both occupy exactly one line -- so
        the rule is the fullest phrasing that still fits on a line. Relevance
        decides what reaches the page and in what order; the column decides
        how it is worded. Those are different questions and were one.
        """
        sizes = [s for s in ("long", "medium", "short")
                 if self.renderable(atom, s)]
        if not sizes:
            return ""

        fits = [s for s in sizes if self._lines(atom, s) == 1]
        if fits and (self._rendered_len(atom, fits[0])
                     >= self._reference_len(atom) * SHORTEN_KEEPS):
            return fits[0]

        # Nothing fits without gutting the bullet, so pay the second line:
        # two lines of evidence beat one line of nothing. `lint` names the
        # atom, because the real repair is a tighter phrasing in the bank.
        return "medium" if self.renderable(atom, "medium") else sizes[-1]

    def _rendered_len(self, atom: Atom, size: str) -> int:
        """Length of what reaches the page, with the claim slots filled in.

        Measuring the template overcharged every bullet that cites a number:
        `{{C.research_isham.speedup}}` is twenty-nine characters of source
        that renders as "216x". Bullets were being charged a second line they
        did not use, which is why pages came out short of the margin while the
        selector believed the budget was spent.
        """
        key = (atom.id, size)
        hit = self._len_cache.get(key)
        if hit is None:
            text, _ = self.resolve(atom.text(size), strict=False)
            hit = self._len_cache[key] = len(text)
        return hit

    def _lines(self, atom: Atom, size: str) -> int:
        # Ceiling, not floor-plus-one: a bullet of exactly WRAP_COLUMN
        # characters fills one line and starts no second one. The off-by-one
        # charged every exactly-full bullet a line it did not use, and the
        # lint called those bullets unfittable when they already fitted.
        width = self._rendered_len(atom, size)
        return max(1, -(-width // WRAP_COLUMN))

    def _cost(self, atom: Atom, size: str, seen_groups: set[str]) -> int:
        cost = self._lines(atom, size)
        if atom.group not in seen_groups:
            cost += 2          # the role heading this bullet renders under
        return cost

    def skills_for(self, jd_text: str, sel: "Selection", *,
                   cap: int = 26, floor: int = 6) -> list[str]:
        """The skills worth printing for THIS application, as one flat list.

        The labelled block this replaces spent eleven lines on every resume,
        most of them on work the page never mentions -- a client-service
        application carried "Graphics: Unreal Engine 5, Blueprints, Blender,
        HLSL", and a backend one carried cell-type deconvolution. Eleven lines
        is four or five bullets that did not fit.

        A skill earns its place one of two ways: the posting asks for it, or a
        bullet that made it onto the page demonstrates it. Anything else is an
        assertion with nothing behind it, which is the one thing a skills
        section should never be -- and it is also what an interviewer picks
        when looking for something to probe.

        The group labels go too. They cost a line each in all but name, and
        nobody reads "Languages:" before "Python". The master file's own order
        is kept instead, so the list still runs from languages through to
        tooling without spending a line saying so.

        `floor` keeps the section from collapsing on a vague posting: if too
        little is earned, the top of the master file tops it back up.
        """
        roles = getattr(self, "roles", {}) or {}
        shown: list[str] = []
        weight: dict[str, int] = {}
        for atom, _size in sel.atoms:
            weight[atom.group] = weight.get(atom.group, 0) + 1

        tagged: list[str] = []
        for atom, size in sel.atoms:
            text, _ = self.resolve(atom.text(size), strict=False)
            shown.append(text or "")
            meta = roles.get(atom.group, {}) or {}
            shown.append(str(meta.get("stack", "")))
            shown.append(str(meta.get("name", "")))
            # Only an entry with a real presence on the page gets to vouch for
            # a skill its bullets never mention. One pinned bullet is not a
            # claim to a specialism: the service desk is pinned onto every
            # resume, and when a single bullet of it could vouch, a
            # quantitative trading resume ended with "macOS, Windows, iOS,
            # Android, ticketing, escalation". Four bullets of it on a client
            # service resume is a different matter, and that is the case this
            # tier was added for.
            if weight.get(atom.group, 0) >= TAG_VOUCH_MIN_BULLETS:
                tagged.extend(str(t) for t in (getattr(atom, "tags", None) or []))
                tagged.extend(str(t) for t in (meta.get("tags") or []))
        page = " ".join(shown).lower()
        tags = " ".join(tagged).lower().replace("_", " ")
        jd = (jd_text or "").lower()

        # Three ways to earn a place, best first.
        #
        #   0  the posting asks for it by name
        #   1  a bullet on the page, or a project's stack, demonstrates it
        #   2  an entry on the page is tagged with it
        #
        # Tier 2 exists because the service desk entry is tagged `ticketing`
        # and `escalation` while none of its four bullets happens to use
        # either word. Without it a client-service resume printed CIBERSORT
        # and WGCNA -- earned honestly, via the capstone's stack -- and left
        # out the two terms that role is actually about.
        tiers: dict[str, int] = {}
        for group in self.skills.values():
            for skill in group:
                key = skill.lower().strip()
                if not key or key in tiers:
                    continue
                if mentions(jd, skill):
                    tiers[key] = 0
                elif mentions(page, skill):
                    tiers[key] = 1
                elif _all_words_tagged(skill, tags):
                    tiers[key] = 2
                else:
                    tiers[key] = 3

        flat = [sk for group in self.skills.values() for sk in group]
        seen: set[str] = set()
        ordered: list[str] = []
        for want in (0, 1, 2):
            for skill in flat:                      # master-file order within a tier
                key = skill.lower().strip()
                if key in seen or tiers.get(key) != want:
                    continue
                seen.add(key)
                ordered.append(skill)

        out = ordered[:max(cap, 0)]

        # The floor is a backstop for a posting too vague to earn anything,
        # not a quota. It never overrules the cap -- it used to, so asking for
        # three skills returned ten -- and it tops up in the master file's own
        # order, which puts the general before the niche, so a thin posting
        # gets languages rather than whatever happens to be listed last.
        want = min(floor, max(cap, 0))
        if len(out) < want:
            have = {o.lower().strip() for o in out}
            for skill in flat:
                if len(out) >= want:
                    break
                key = skill.lower().strip()
                if key not in have:
                    have.add(key)
                    out.append(skill)
        return out

    def select(self, jd_text: str, *, family: str = "default",
               line_budget: int = 38, max_per_group: int = 4) -> Selection:
        """Fit the best subset of a large bank onto one page.

        Two phases, because "always include this" and "include what fits" are
        different requirements and mixing them loses the first one:

        1. **Pinned atoms.** Non-negotiables claim their space before anything
           competes for it. If they cannot all fit, the overflow is reported by
           name rather than silently dropped -- that is a signal the pin set is
           too big for one page, and only you can decide what gives.
        2. **Everything else**, greedily by relevance per line consumed.

        `max_per_group` stops one role with twelve bullets from eating the page.
        """
        jd = (jd_text or "").lower()
        # The dot is in the class so "node.js" and "three.js" survive as one
        # token — but that also swallows a sentence-ending full stop, so
        # "we use gRPC." tokenised to "grpc." and matched no tag at all. Any
        # technology named at the end of a sentence was invisible to both
        # relevance scoring and gap detection. Strip the dots at the edges and
        # keep the ones inside.
        jd_tokens = {t.strip(".") for t in re.findall(r"[a-z][a-z0-9+#.]{1,}", jd)}
        jd_tokens.discard("")

        scored = [(self.relevance(a, jd, jd_tokens, family), a) for a in self.atoms]
        scored.sort(key=lambda p: -p[0])

        chosen: list[tuple[Atom, str]] = []
        seen_groups: set[str] = set()
        per_section: dict[str, int] = {}
        per_group: dict[str, int] = {}
        claims_used: dict[str, str] = {}
        used = 0
        pinned_dropped: list[str] = []

        chosen_ids: set[str] = set()

        def try_add(atom: Atom, rel: float, *, force: bool) -> bool:
            nonlocal used
            if atom.id in chosen_ids:
                return False
            limits = self.sections.get(atom.section, {})
            if not force:
                if per_section.get(atom.section, 0) >= int(limits.get("max_atoms", 6)):
                    return False
                if per_group.get(atom.group, 0) >= max_per_group:
                    return False
            size = self._best_size(atom)
            if not size:
                return False
            cost = self._cost(atom, size, seen_groups)
            if used + cost > line_budget:
                # Try the shortest renderable phrasing before giving up.
                for fallback in ("short", "medium"):
                    if self.renderable(atom, fallback):
                        c2 = self._cost(atom, fallback, seen_groups)
                        if used + c2 <= line_budget:
                            size, cost = fallback, c2
                            break
                else:
                    return False
                if used + cost > line_budget:
                    return False
            _, used_claims = self.resolve(atom.text(size), strict=False)
            claims_used.update(used_claims)
            chosen.append((atom, size))
            chosen_ids.add(atom.id)
            seen_groups.add(atom.group)
            per_section[atom.section] = per_section.get(atom.section, 0) + 1
            per_group[atom.group] = per_group.get(atom.group, 0) + 1
            used += cost
            return True

        # phase 1 -- non-negotiables
        for rel, a in [(r, a) for r, a in scored if a.pinned]:
            if not try_add(a, rel, force=True):
                pinned_dropped.append(a.id)

        # phase 1b -- the floor each section is worth keeping
        #
        # `min_atoms` had been in the config since the bank was written and
        # nothing read it, which only became visible once the page filled up.
        # Greedy relevance is right about order and wrong about presence: for
        # a backend posting, olympiad medals score near zero against the
        # description and lose their place to a fourth project bullet. They
        # are not evidence of backend skill; they are evidence of the
        # candidate, and a resume with no Education content under the degree
        # line is not a better resume.
        for section, limits in self.sections.items():
            need = int(limits.get("min_atoms", 0))
            for rel, a in scored:
                if per_section.get(section, 0) >= need:
                    break
                if a.section == section:
                    try_add(a, rel, force=True)

        # phase 2 -- best of the rest
        for rel, a in scored:
            if a.pinned:
                continue
            try_add(a, rel, force=False)

        # Fit order is not reading order. The two phases above decide what
        # *appears*; a pin means "must appear", not "must appear first", and
        # letting fit order reach the page led a backend posting with finite
        # geometry and a graphics posting with a help desk -- in both cases
        # because that entry happened to carry a pin.
        #
        # Entries are ordered by how well the role answers this posting, and
        # bullets stay in the order they were written within their entry: a
        # role's bullets are prose, and sorting them scrambles the narrative.
        best_for_group: dict[tuple[str, str], float] = {}
        for rel, a in scored:
            key = (a.section, a.group)
            if rel > best_for_group.get(key, float("-inf")):
                best_for_group[key] = rel
        written = {a.id: i for i, a in enumerate(self.atoms)}
        chosen.sort(key=lambda pair: (
            -best_for_group[(pair[0].section, pair[0].group)],
            written[pair[0].id],
        ))

        gaps = self._gaps(jd_tokens)
        coverage = len(chosen) / max(1, len(self.atoms))
        return Selection(chosen, round(coverage, 3), gaps, claims_used,
                         dropped=len(self.atoms) - len(chosen),
                         pinned_dropped=pinned_dropped)

    def _gaps(self, jd_tokens: set[str]) -> list[str]:
        have = {t for a in self.atoms for t in a.tags}
        have |= {s.lower() for group in self.skills.values() for s in group}
        # A curated vocabulary, not every token in the posting: "collaborate"
        # and "fast-paced" are not gaps, and reporting them would bury the four
        # that matter. The trade-off is that an empty result means "nothing from
        # this list is missing", not "no gaps at all".
        #
        # Kept deliberately wide across the three target areas, because this
        # list is the whole of the "what should I build next" signal — a name
        # missing from it is a gap the system structurally cannot report.
        interesting = {
            # languages
            "rust", "go", "golang", "scala", "kotlin", "swift", "ruby", "php",
            "elixir", "haskell", "matlab", "julia", "perl", "lua", "ocaml",
            "clojure", "erlang", "fortran", "assembly", "verilog", "vhdl",
            # infrastructure and platform
            "kubernetes", "terraform", "ansible", "helm", "docker", "nomad",
            "aws", "gcp", "azure", "lambda", "serverless", "cloudformation",
            "prometheus", "grafana", "datadog", "opentelemetry", "envoy",
            "nginx", "bazel", "ci/cd", "jenkins", "argo",
            # data
            "kafka", "spark", "flink", "airflow", "dbt", "snowflake",
            "databricks", "clickhouse", "cassandra", "elasticsearch", "redis",
            "mongodb", "dynamodb", "bigquery", "hadoop", "parquet", "duckdb",
            # ml and ai
            "tensorflow", "jax", "cuda", "triton", "vllm", "ray", "mlops",
            "onnx", "tensorrt", "huggingface", "langchain", "rag", "embeddings",
            "vector", "pinecone", "weaviate", "faiss", "quantization",
            "distillation", "finetuning", "lora", "rlhf", "diffusion",
            "transformers", "reinforcement", "bayesian", "causal",
            # security
            "pentest", "pentesting", "fuzzing", "reverse", "malware", "siem",
            "soc", "owasp", "burp", "metasploit", "wireshark", "nmap", "ghidra",
            "ida", "exploit", "cryptography", "pki", "zerotrust", "threat",
            "forensics", "incident", "vulnerability", "appsec", "devsecoops",
            "devsecops", "sast", "dast", "fuzzer", "sandboxing",
            # quant and numerics
            "quantlib", "numba", "cython", "eigen", "blas", "lapack", "mpi",
            "openmp", "simd", "montecarlo", "stochastic", "timeseries",
            "kdb", "q", "optiver", "arbitrage",
            # web and api
            "graphql", "grpc", "protobuf", "websocket", "oauth", "webassembly",
            "wasm", "svelte", "vue", "angular", "nextjs", "tailwind",
        }
        return sorted((jd_tokens & interesting) - have)


FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("security", ("security", "appsec", "infosec", "penetration test", "pentest",
                  "vulnerability", "red team", "threat model", "malware", "forensics")),
    ("quant",    ("quantitative", "quant ", "trading", "trader", "market making",
                  "systematic", "derivative", "hedge fund", "proprietary trading")),
    # " ai " is padded on both sides so it cannot match inside "said" or
    # "maintain"; the title and description are themselves padded before
    # matching so a title *ending* in "- AI Infrastructure" still hits.
    ("ai_ml",    ("machine learning", "ml engineer", "deep learning", "llm",
                  "nlp", "research scientist", "computer vision", "pytorch",
                  "tensorflow", "neural network", " ai ", "artificial intelligence",
                  "generative ai", "genai", "ai/ml", " ml ")),
    ("graphics", ("graphics", "rendering", "unreal", "unity", "shader", "vfx",
                  "game engine", "real-time 3d")),
    ("research", ("research assistant", "phd", "publication", "laboratory",
                  "dissertation", "peer-reviewed")),
    # Last, so a specialist qualifier always wins: "Security Consultant" is a
    # security job. Every phrase here is multi-word for the same reason --
    # bare "consultant" would capture "Software Engineering Consultant", which
    # is an engineering job that happens to be billed to a client.
    #
    # Without this family, every non-engineering posting fell through to
    # `default`, which leads with whatever is strongest technically. A client
    # service role at an expert network therefore received a resume led by
    # finite-geometry research and Unreal Engine, while the club presidency,
    # the service desk and the teaching assistantship -- the whole of the
    # relevant evidence -- sat unselected in the bank.
    ("client",   ("client service", "client services", "client relations",
                  "client development", "summer associate", "business analyst",
                  "customer success", "account manager", "account management",
                  "business development", "management consulting",
                  "strategy consulting", "strategy analyst", "client partner")),
]

# How many DISTINCT keywords a job description must contain before it may claim
# a specialist family on its own.
DESCRIPTION_FAMILY_THRESHOLD = 2

# A title that names a general engineering role and nothing more. The company's
# domain is not the role's discipline: an agentic-AI startup hiring a
# "Software Engineering Intern" to write Python and React wants a software
# engineer, and its description will still be dense with "ML" and "AI" because
# that is what the company sells. Reading the description there produces a
# machine-learning resume for a backend job.
GENERIC_TITLE_MARKERS = (
    "software engineer", "software engineering", "software developer",
    "software development", "swe ", "full stack", "full-stack", "fullstack",
    "backend", "back-end", "frontend", "front-end", "web developer",
    "web development", "platform engineer", "product engineer",
)


def job_family(job: dict) -> str:
    """Which experience should lead, from the job text.

    The title decides when it can, because the title is what the job *is*.
    The description only gets a vote when the title is generic, and then it
    needs more than one distinct signal.

    Both rules come from the same real posting: a startup titled "Software
    Engineering Intern" that wanted Python and React described its customers
    as "ML researchers, quants, and data scientists". That lone incidental
    "quants" matched, and the candidate's resume came out led by competition
    mathematics and a crypto-arbitrage project instead of his agentic Python
    system — for a backend job that was asking for exactly that system.

    A misread family is not a cosmetic problem: it silently sends the wrong
    resume, and nothing downstream can detect it.
    """
    # Padded so a keyword carrying its own word boundaries, like " ai ", still
    # matches at the very start or end of the text.
    title = f" {str(job.get('title', '')).lower()} "
    desc = f" {str(job.get('description', '')).lower()} "

    for family, keywords in FAMILY_KEYWORDS:
        if any(k in title for k in keywords):
            return family

    # A plain engineering title with no specialist qualifier settles it. The
    # description does not get a vote, because at a specialist company it is
    # describing the product rather than the job.
    if any(m in title for m in GENERIC_TITLE_MARKERS):
        return "default"

    best, best_hits = "default", 0
    for family, keywords in FAMILY_KEYWORDS:
        hits = sum(1 for k in keywords if k in desc)
        if hits > best_hits:
            best, best_hits = family, hits

    return best if best_hits >= DESCRIPTION_FAMILY_THRESHOLD else "default"
