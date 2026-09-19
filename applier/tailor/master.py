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

KNOWN_SECTIONS = {
    "experience": "experience",
    "projects": "projects",
    "leadership": "leadership",
    "education": "education",
    "honors": "education",
    "awards": "education",
    "skills": "skills",
}

# Numerals that are part of a name rather than a measurement. Left alone.
NUMERAL_SKIP = re.compile(
    r"^(?:19|20)\d{2}$"                      # years
    r"|^[1-5]$"                              # trivial counts / GPA scale
)
# Capture the unit alongside the number so "30 FPS" stays one claim rather
# than a bare "30" that could mean anything.
UNITS = (r"FPS|fps|days?|hours?|minutes?|mins?|seconds?|secs?|weeks?|months?|years?"
         r"|students?|attendees?|tickets?|members?|languages?|applicants?|courses?"
         r"|commits?|contributors?|venues?|events?|organizations?|organisations?")
NUMERAL_RE = re.compile(
    r"(?<![\w.$])("
    r"~?\$?\d[\d,]*(?:\.\d+)?"          # the number
    r"(?:\s*[-\u2013]\s*\d[\d,]*(?:\.\d+)?)?"   # optional range: 70-200
    r"(?:\s*(?:%|x\b|\+|!))?"             # optional suffix: % x + !
    r"(?:\s+(?:" + UNITS + r"))?"         # optional unit word
    r")")


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



def _claim_name(value: str) -> str:
    """A stable claim name derived from the value itself.

    Sequential names (n1, n2, ...) were a latent bug: the counter reset for each
    bullet, so two bullets in the same entry both minted "n1" and the second
    silently overwrote the first -- binding one bullet's prose to another
    bullet's number. A value-derived name cannot collide unless the values are
    genuinely identical, in which case sharing a claim is correct.
    """
    v = value.strip().lower()
    v = v.replace("%", "pct").replace("+", "plus").replace("$", "usd")
    v = re.sub(r"[^a-z0-9]+", "_", v).strip("_")
    return ("n_" + v)[:40] or "n_value"


def extract_numbers(text: str, prefix: str, existing: dict) -> tuple[str, dict]:
    """Replace inline numerals with claim slots, minting claims as needed.

    A number already present in the ledger keeps its existing key and status —
    so anything you retired stays retired across re-imports. That is the whole
    point: the importer must never quietly resurrect a metric you removed.
    """
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
        if NUMERAL_SKIP.match(tok.replace(",", "")):
            return tok
        # A version number is part of a product's name, not a measurement.
        # "Unreal Engine 5.3" and "Next.js 15" should stay literal rather than
        # filling the ledger with figures nobody could "defend".
        before = text[max(0, m.start() - 24):m.start()].rstrip()
        if re.fullmatch(r"\d+\.\d+", tok) and re.search(r"[A-Za-z][A-Za-z.]*\s*$", before):
            return tok
        key = lookup(tok)
        if key:
            sect, name = key.split(".", 1)
            claim = (existing.get(sect) or {}).get(name) or {}
            if str(claim.get("status")) == "RETIRED":
                # The number is gone on purpose. Drop it from the phrasing
                # rather than reintroducing it.
                return ""
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

    return NUMERAL_RE.sub(repl, text), minted


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
    bank_dir.mkdir(parents=True, exist_ok=True)

    claims_path = bank_dir / "claims.yaml"
    existing_claims: dict = {}
    if claims_path.exists():
        existing_claims = yaml.safe_load(claims_path.read_text(encoding="utf-8")) or {}

    atoms: list[dict] = []
    roles: dict[str, dict] = {}
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

            long_text, minted = extract_numbers(b["text"], prefix, ledger)
            _merge(new_claims, minted)
            _merge(ledger, minted)

            phrasings = {"long": long_text}
            variants = list(b["variants"])
            if variants:
                med, m2 = extract_numbers(variants[0], prefix, ledger)
                _merge(new_claims, m2)
                _merge(ledger, m2)
                phrasings["medium"] = med
            if len(variants) > 1:
                sh, m3 = extract_numbers(variants[1], prefix, ledger)
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
