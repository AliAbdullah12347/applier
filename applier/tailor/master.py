"""The master resume: one human-editable file that holds everything.

You maintain `config/bank/master_resume.md` — every role, every project, every
bullet you have ever written, with no length limit. You add to it whenever you
like. The tailoring engine picks a subset per job and fits one page.

    applier bank import     master_resume.md -> atoms.yaml + claims.yaml
    applier bank lint       check the bank is renderable and truthful
    applier bank preview    what a given job would actually select

Why an importer rather than editing YAML directly
-------------------------------------------------
The engine needs structure — tags, provenance, a number ledger. You need
something you can open and add a bullet to in ten seconds. The importer is the
bridge: you write a resume, it produces the structure.

Number handling is the important part. You write bullets naturally, with the
numbers inline. The importer pulls every numeral out into `claims.yaml` and
replaces it with a slot, so the renderer still physically cannot emit an
unregistered figure. New numbers arrive as `status: needs_check`, which means
they render but are flagged for you to confirm. Numbers you have already
retired stay retired — re-importing never resurrects them.

Format
------
    ## Experience                       <- section
    ### Research Assistant — Computational Algebra    [pin]
    org: Example University | Prof. A. Supervisor
    location: City, ST
    dates: May 2026 -- Present
    tags: algorithms, python, research, performance
    lead_for: ai_ml, quant, research
    id: exp_research

    - Cut a classification job from 3 days to 20 minutes (~216x) by precomputing ...
      ~ Cut a classification job from 3 days to 20 minutes (~216x).
    - Applied field arithmetic and pruning to shrink the search space    [pin]

`[pin]` on a heading means the role always appears. `[pin]` on a bullet means
that bullet always appears. Those are the non-negotiables.
`~` lines are shorter alternatives of the bullet above, used when space is tight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
ENTRY_RE = re.compile(r"^###\s+(.+?)\s*$")
META_RE = re.compile(r"^([a-z_]+):\s*(.*)$")
BULLET_RE = re.compile(r"^[-*]\s+(.+?)\s*$")
VARIANT_RE = re.compile(r"^\s+(~{1,2})\s*(.+?)\s*$")
PIN_RE = re.compile(r"\s*\[pin\]\s*$", re.I)

# Only four sections are rendered by a resume template, plus skills. A heading
# that maps to none of them parses fine, imports fine, lints clean — and then
# never appears on any resume. Silent content loss is the worst failure this
# file can produce, so the alias list is generous and anything still unmatched
# is reported rather than dropped (see `unknown_sections` in build_bank, and
# the renderable-section check in Bank.lint).
RENDERABLE_SECTIONS = {"experience", "projects", "leadership", "education"}

KNOWN_SECTIONS = {
    "experience": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "employment": "experience",
    "employment history": "experience",
    "research experience": "experience",

    "projects": "projects",
    "personal projects": "projects",
    "selected projects": "projects",
    "technical projects": "projects",
    "research": "projects",
    "publications": "projects",
    "papers": "projects",

    "leadership": "leadership",
    "leadership & activities": "leadership",
    "activities": "leadership",
    "involvement": "leadership",
    "extracurriculars": "leadership",
    "service": "leadership",

    "education": "education",
    "honors": "education",
    "honours": "education",
    "awards": "education",
    "honors & awards": "education",
    "honours & awards": "education",
    "awards & honors": "education",
    "awards and honors": "education",
    "honors and awards": "education",

    "skills": "skills",
    "technical skills": "skills",
    "skills & tools": "skills",
    "skills and tools": "skills",
    "tools": "skills",
}

# Numerals that are part of a name rather than a measurement. Left alone.
NUMERAL_SKIP = re.compile(
    r"^(?:19|20)\d{2}$"                      # years
    r"|^[1-5]$"                              # trivial counts / GPA scale
)
# Capture the unit alongside the number so "30 FPS" stays one claim rather
# than a bare "30" that could mean anything.
#
# The word boundary after the unit group is load-bearing: without it "secs?"
# matched the
# "sec" inside "6 sections", minting a claim literally valued "6 sec". It
# round-tripped by accident (slot + "tions"), but the claims ledger exists to
# be reviewed by a human, and "could I defend '6 sec'?" is not a question
# anyone can answer.
UNITS = (r"FPS|fps|days?|hours?|minutes?|mins?|seconds?|secs?|weeks?|months?|years?"
         r"|students?|attendees?|tickets?|members?|languages?|applicants?|courses?"
         r"|commits?|contributors?|venues?|events?|organizations?|organisations?")
NUMERAL_RE = re.compile(
    r"(?<![\w.$])("
    r"~?\$?\d[\d,]*(?:\.\d+)?"          # the number
    r"(?:\s*[-\u2013]\s*\d[\d,]*(?:\.\d+)?)?"   # optional range: 70-200
    r"(?:\s*(?:%|x\b|\+|!))?"             # optional suffix: % x + !
    r"(?:\s+(?:" + UNITS + r")\b)?"        # optional unit word, whole-word only
    r")")


def is_literal_numeral(tok: str, before: str = "") -> bool:
    """Whether a numeral legitimately stays literal instead of becoming a claim.

    The importer and `Bank.lint` must agree on this exactly, or the bank
    becomes unrenderable with no remedy: the importer declines to mint a claim
    for a numeral, and lint then demands one for the literal it left behind.
    That happened twice — on "under 2." and on "Unreal Engine 5.3" — so the
    rule lives here once and both callers use it.

    `before` is the text immediately preceding the numeral, which is what
    distinguishes a version number from a measurement.
    """
    t = tok.strip().rstrip(".,;:")
    if not t:
        return True
    if NUMERAL_SKIP.match(t.replace(",", "")):
        return True
    # A version number is part of a product's name, not a measurement, so
    # "Unreal Engine 5.3" and "Python 3.11" stay literal rather than filling
    # the ledger with figures nobody could defend in an interview.
    #
    # The dot is required, and that is deliberate: a bare "15" after a word
    # cannot be told apart from "served 15 customers", so relaxing this would
    # exempt nearly every real measurement. A bare major version of 1-5 is
    # already covered by the trivial-count rule above; anything higher simply
    # becomes a claim, which renders correctly either way.
    if re.fullmatch(r"\d+\.\d+", t) and re.search(r"[A-Za-z][A-Za-z.]*\s*$", before.rstrip()):
        return True
    return False


class MasterError(RuntimeError):
    pass


@dataclass
class MasterEntry:
    section: str
    heading: str
    entry_id: str
    meta: dict[str, Any] = field(default_factory=dict)
    bullets: list[dict] = field(default_factory=list)   # {text, variants[], pinned}
    pinned: bool = False


def slugify(text: str) -> str:
    t = re.sub(r"[^\w\s-]", "", text.lower())
    t = re.sub(r"[\s-]+", "_", t).strip("_")
    return t[:48] or "entry"


# --------------------------------------------------------------------------- #
def parse(path: Path) -> tuple[list[MasterEntry], dict[str, list[str]]]:
    """Parse the master resume into entries plus a skills map."""
    if not path.exists():
        raise MasterError(f"No master resume at {path}")

    entries: list[MasterEntry] = []
    skills: dict[str, list[str]] = {}
    unknown_sections: set[str] = set()
    section = ""
    cur: MasterEntry | None = None
    in_skills = False

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("<!--") or line.lstrip().startswith("#!"):
            continue

        m = SECTION_RE.match(line)
        if m:
            name = m.group(1).strip().lower()
            section = KNOWN_SECTIONS.get(name, name)
            if section not in KNOWN_SECTIONS.values():
                unknown_sections.add(m.group(1).strip())
            in_skills = section == "skills"
            cur = None
            continue

        if in_skills:
            if ":" in line:
                group, items = line.split(":", 1)
                vals = [v.strip() for v in items.split(",") if v.strip()]
                if vals:
                    skills[group.strip().lstrip("-* ")] = vals
            continue

        m = ENTRY_RE.match(line)
        if m:
            head = m.group(1)
            pinned = bool(PIN_RE.search(head))
            head = PIN_RE.sub("", head).strip()
            cur = MasterEntry(section=section or "experience", heading=head,
                              entry_id=slugify(head), pinned=pinned)
            entries.append(cur)
            continue

        if cur is None:
            continue

        m = VARIANT_RE.match(raw)
        if m and cur.bullets:
            cur.bullets[-1]["variants"].append(m.group(2))
            continue

        m = BULLET_RE.match(line)
        if m:
            text = m.group(1)
            bpin = bool(PIN_RE.search(text))
            text = PIN_RE.sub("", text).strip()
            cur.bullets.append({"text": text, "variants": [], "pinned": bpin})
            continue

        m = META_RE.match(line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if key in ("tags", "lead_for"):
                cur.meta[key] = [v.strip() for v in val.split(",") if v.strip()]
            elif key == "id":
                cur.entry_id = slugify(val)
            elif key == "pin":
                cur.pinned = val.strip().lower() in ("true", "yes", "1")
            else:
                cur.meta[key] = val
            continue

    if unknown_sections:
        # Printed, not raised: the rest of the file is still worth importing,
        # and Bank.lint will refuse to render the affected atoms anyway.
        names = ", ".join(sorted(unknown_sections))
        print(f"  [master] WARNING: unrecognised section heading(s): {names}")
        print("           Content under these will not appear on any resume. "
              "Use Experience, Projects, Leadership, Education or Skills.")
    return entries, skills


# --------------------------------------------------------------------------- #

def _shape(value: str) -> str:
    """The identity of a number: its digits AND its unit.

    Two bugs came from getting this wrong, and both silently bound unrelated
    facts to a single claim, so editing one would change the other:

      digits only      "70" from "70-200 attendees" bound to an unrelated "70%"
      digits + suffix  "30 students" bound to "30 FPS"

    A number means nothing without its unit, so the unit is part of the key.
    Everything else -- spaces, commas, tildes, currency -- is noise.
    """
    v = value.strip().lower()
    v = re.sub(r"[\s,~$]", "", v)
    return re.sub(r"[^0-9a-z%+!.-]", "", v)



# Every character `_shape` keeps must map to something distinct here, or two
# different numbers can land on one name. `!` was missing, which is how "8" and
# "8!" in the same bullet both became `n_8` — the second overwrote the first,
# and "8-arc classification" rendered as "8!-arc classification" on a finished
# PDF. Adding a suffix to NUMERAL_RE means adding it here too.
_NAME_CHARS = {
    "%": "pct", "+": "plus", "$": "usd", "!": "fact",
    ".": "p", "-": "_to_",
}


def _claim_name(value: str) -> str:
    """A stable claim name derived from the value's *identity*.

    Sequential names (n1, n2, ...) were a latent bug: the counter reset for each
    bullet, so two bullets in the same entry both minted "n1" and the second
    silently overwrote the first -- binding one bullet's prose to another
    bullet's number. A value-derived name cannot collide unless the values are
    genuinely identical, in which case sharing a claim is correct.

    The name is derived from `_shape`, not from the raw value, so that two
    numbers collide on a name if and only if they collide on identity. Keeping
    those two definitions in step is the whole point: a name collision binds
    unrelated facts together, and that is invisible until it reaches a PDF.
    """
    shape = _shape(value)
    out = []
    for ch in shape:
        out.append(_NAME_CHARS.get(ch, ch if ch.isalnum() else "_"))
    v = re.sub(r"_+", "_", "".join(out)).strip("_")
    return ("n_" + v)[:40] if v else "n_value"


def extract_numbers(text: str, prefix: str, existing: dict,
                    retired_out: list | None = None) -> tuple[str, dict]:
    """Replace inline numerals with claim slots, minting claims as needed.

    A number already present in the ledger keeps its existing key and status —
    so anything you retired stays retired across re-imports. That is the whole
    point: the importer must never quietly resurrect a metric you removed.

    Returns the rewritten text, the newly minted claims, and any RETIRED claims
    the text still refers to. The caller refuses on the third.
    """
    retired_hits: list[tuple[str, str]] = []
    minted: dict[str, dict] = {}
    value_to_key: dict[str, str] = {}
    digits_to_keys: dict[str, set[str]] = {}
    for section, claims in (existing or {}).items():
        if not isinstance(claims, dict):
            continue
        for name, claim in claims.items():
            if isinstance(claim, dict) and "value" in claim:
                val = str(claim["value"]).strip()
                key = f"{section}.{name}"
                value_to_key.setdefault(val, key)
                digits_to_keys.setdefault(_shape(val), set()).add(key)

    def lookup(tok: str) -> str | None:
        """Exact match first; then digits-only, but only when unambiguous.

        Falling back on digits alone is what lets "30 FPS" in the text find the
        existing "30 FPS" claim even if the tokeniser clipped the unit. It is
        deliberately refused when two claims share the same digits, because
        silently binding a bullet to the wrong number is worse than minting a
        duplicate you can merge later.
        """
        if tok in value_to_key:
            return value_to_key[tok]
        d = _shape(tok)
        if not d:
            return None
        cands = digits_to_keys.get(d, set())
        return next(iter(cands)) if len(cands) == 1 else None


    def repl(m: re.Match) -> str:
        tok = m.group(1).strip()
        # One shared rule with Bank.lint -- see is_literal_numeral.
        if is_literal_numeral(tok, text[max(0, m.start() - 24):m.start()]):
            return tok
        key = lookup(tok)
        if key:
            sect, name = key.split(".", 1)
            claim = (existing.get(sect) or {}).get(name) or {}
            if str(claim.get("status")) == "RETIRED":
                # The number is gone on purpose -- but deleting it from the
                # sentence is not a safe default, because the sentence is
                # usually ABOUT the number. "Increased build stability by 30%
                # by resolving C# errors" silently became "Increased build
                # stability by  by resolving C# errors", and lint called it
                # clean, so mangled prose would have been typeset onto a real
                # application.
                #
                # Record it and let the caller refuse. The bullet has to be
                # rewritten by a human; there is no correct automatic repair.
                retired_hits.append((tok, key))
                return tok
            return "{{C." + key + "}}"
        name = _claim_name(tok)
        minted.setdefault(prefix, {})[name] = {
            "value": tok,
            "evidence": "IMPORTED from master_resume.md -- confirm you can defend this.",
            "status": "needs_check",
        }
        value_to_key[tok] = f"{prefix}.{name}"
        digits_to_keys.setdefault(_shape(tok), set()).add(f"{prefix}.{name}")
        return "{{C." + f"{prefix}.{name}" + "}}"

    out = NUMERAL_RE.sub(repl, text)
    if retired_out is not None:
        retired_out.extend(retired_hits)
    return out, minted


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst.setdefault(k, v)
    return dst


# --------------------------------------------------------------------------- #
def build_bank(master_path: Path, bank_dir: Path, *, write: bool = True) -> dict:
    """master_resume.md -> atoms.yaml + claims.yaml. Returns a summary."""
    entries, skills = parse(master_path)

    # A file with content but no parseable structure used to import
    # "successfully" — 0 entries, 0 atoms — and then lint clean, vacuously,
    # because there was nothing to find fault with. You would believe it had
    # worked until a render came back empty.
    #
    # This is what happens when a master resume is copied out of a rendered
    # markdown view instead of the raw code block: the `##` and `###` markers
    # are gone, every metadata line is folded into one paragraph, and the file
    # is text rather than structure. Refuse it, and say which marker is absent.
    # `not skills` matters: a file holding only a `## Skills` block has no
    # entries by design and is perfectly valid. The failure being caught here
    # is a file that yielded *nothing at all*.
    if not entries and not skills:
        raw = master_path.read_text(encoding="utf-8")
        body = [ln for ln in raw.splitlines() if ln.strip()]
        if body:
            hints = []
            if not any(ln.lstrip().startswith("## ") for ln in body):
                hints.append("no '## Section' headings")
            if not any(ln.lstrip().startswith("### ") for ln in body):
                hints.append("no '### Entry' headings")
            if not any(re.match(r"^[-*]\s+", ln) for ln in body):
                hints.append("no '- bullet' lines")
            detail = "; ".join(hints) or "the structure did not match the expected format"
            raise MasterError(
                f"{master_path.name} has {len(body)} lines of text but produced no "
                f"entries: {detail}.\n"
                f"  If you copied this out of a chat, copy the RAW text from the "
                f"code block rather than the rendered view — rendering strips the "
                f"'#' markers and folds the metadata lines together."
            )

    bank_dir.mkdir(parents=True, exist_ok=True)

    claims_path = bank_dir / "claims.yaml"
    existing_claims: dict = {}
    if claims_path.exists():
        existing_claims = yaml.safe_load(claims_path.read_text(encoding="utf-8")) or {}

    atoms: list[dict] = []
    roles: dict[str, dict] = {}
    all_retired_hits: dict[str, list] = {}
    new_claims: dict = {}
    pinned_count = 0

    for e in entries:
        prefix = e.entry_id
        if e.section == "projects":
            roles[prefix] = {
                "name": e.heading,
                "stack": e.meta.get("stack", ""),
                "dates": e.meta.get("dates", ""),
                "url": e.meta.get("url", ""),
                "section": e.section,
            }
        else:
            roles[prefix] = {
                "title": e.heading,
                "org": e.meta.get("org", ""),
                "location": e.meta.get("location", ""),
                "dates": e.meta.get("dates", ""),
                "section": e.section,
            }
        if e.pinned:
            roles[prefix]["pinned"] = True

        for i, b in enumerate(e.bullets, 1):
            # Thread the claims minted by each phrasing forward into the next.
            # Without this, a variant that mentions a number the long form
            # phrases differently mints its own claim, the caller discards it,
            # and the variant is left pointing at a slot that does not exist --
            # which the lint gate then refuses to render.
            ledger = yaml.safe_load(yaml.safe_dump(existing_claims)) if existing_claims else {}
            _merge(ledger, new_claims)

            retired_here: list[tuple[str, str]] = []
            all_retired_hits.setdefault(e.entry_id, [])
            long_text, minted = extract_numbers(b["text"], prefix, ledger, retired_here)
            _merge(new_claims, minted)
            _merge(ledger, minted)

            if retired_here:
                all_retired_hits[e.entry_id].append((b["text"], retired_here))

            phrasings = {"long": long_text}
            variants = list(b["variants"])
            if variants:
                med, m2 = extract_numbers(variants[0], prefix, ledger, retired_here)
                _merge(new_claims, m2)
                _merge(ledger, m2)
                phrasings["medium"] = med
            if len(variants) > 1:
                sh, m3 = extract_numbers(variants[1], prefix, ledger, retired_here)
                _merge(new_claims, m3)
                phrasings["short"] = sh
            if "medium" not in phrasings:
                phrasings["medium"] = long_text
            if "short" not in phrasings:
                phrasings["short"] = phrasings["medium"]

            atoms.append({
                "id": f"{prefix}_{i}",
                "section": e.section,
                "group": prefix,
                "tags": e.meta.get("tags", []) or [],
                "lead_for": e.meta.get("lead_for", []) or [],
                "pinned": bool(b["pinned"] or e.pinned),
                "phrasings": phrasings,
            })
            if b["pinned"] or e.pinned:
                pinned_count += 1

    # A bullet that still cites a RETIRED metric cannot be repaired
    # automatically: the sentence is usually *about* the number, so deleting it
    # leaves "Increased build stability by  by resolving C# errors" — which
    # linted clean and would have been typeset onto a real application.
    #
    # Refuse, and name the bullet. Reinstating a metric is a decision only the
    # person who withdrew it can make, and it is one line in claims.yaml.
    if any(hits for hits in all_retired_hits.values()):
        lines = []
        for entry_id, hits in all_retired_hits.items():
            for bullet, refs in hits:
                vals = ", ".join(sorted({f"{tok!r} ({key})" for tok, key in refs}))
                lines.append(f"  {entry_id}: {vals}\n      {bullet[:110]}")
        raise MasterError(
            "This master resume cites metric(s) you previously RETIRED:\n"
            + "\n".join(lines)
            + "\n\n  A retired number is one you decided you could not defend. "
              "Either rewrite the bullet without it, or, if you have since "
              "confirmed the figure, change its status in "
              "config/bank/claims.yaml from RETIRED to verified and re-import."
        )

    merged_claims = yaml.safe_load(yaml.safe_dump(existing_claims)) if existing_claims else {}
    _merge(merged_claims, new_claims)

    sections = {
        "education": {"order": 1, "min_atoms": 0, "max_atoms": 3},
        "experience": {"order": 2, "min_atoms": 3, "max_atoms": 10},
        "projects": {"order": 3, "min_atoms": 1, "max_atoms": 5},
        "leadership": {"order": 4, "min_atoms": 0, "max_atoms": 2},
    }

    doc = {
        "atoms": atoms,
        "sections": sections,
        "skills": skills,
        "roles": roles,
    }

    if write:
        header = (
            "# GENERATED from master_resume.md by `applier bank import`.\n"
            "# Do not edit this file by hand -- your changes will be overwritten.\n"
            "# Edit config/bank/master_resume.md instead, then re-run the import.\n\n"
        )
        (bank_dir / "atoms.yaml").write_text(
            header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110),
            encoding="utf-8", newline="")
        if new_claims:
            claims_path.write_text(
                yaml.safe_dump(merged_claims, sort_keys=False, allow_unicode=True, width=110),
                encoding="utf-8", newline="")

    return {
        "entries": len(entries),
        "atoms": len(atoms),
        "pinned": pinned_count,
        "skill_groups": len(skills),
        "new_claims": sum(len(v) for v in new_claims.values()),
        "needs_check": [
            f"{s}.{n}" for s, cl in merged_claims.items() if isinstance(cl, dict)
            for n, c in cl.items()
            if isinstance(c, dict) and c.get("status") == "needs_check"
        ],
        "retired_dropped": sum(
            1 for s, cl in (existing_claims or {}).items() if isinstance(cl, dict)
            for c in cl.values() if isinstance(c, dict) and c.get("status") == "RETIRED"
        ),
    }
