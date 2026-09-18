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

SLOT_RE = re.compile(r"\{\{\s*C\.([A-Za-z0-9_.]+)\s*\}\}")
# Bare numerals that must come from claims instead. Ignores things like "8-arc"
# that are part of a name, and ordinals inside words.
BARE_NUMERAL_RE = re.compile(r"(?<![\w.\-])\d[\d,.]*\s*(?:%|x\b|\+)?")


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
        for a in self.atoms:
            if not a.phrasings:
                problems.append(f"{a.id}: no phrasings")
            for size, text in a.phrasings.items():
                stripped = SLOT_RE.sub("", text)
                for m in BARE_NUMERAL_RE.finditer(stripped):
                    tok = m.group(0).strip()
                    if not tok:
                        continue
                    # These are not measurements and must stay literal, or the
                    # ledger fills with noise and the real check gets ignored:
                    #   years (2026), trivial counts (1-5, as in "3D", "one of 4"),
                    #   and digits glued to a letter (3D, v5.3, C4).
                    tail = stripped[m.end():m.end() + 1]
                    if re.fullmatch(r"(19|20)\d{2}", tok):
                        continue
                    if re.fullmatch(r"[1-5]", tok):
                        continue
                    if tail.isalpha():
                        continue
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
        rel = hits / max(2.0, len(atom.tags) or 1)
        if family in atom.lead_for:
            rel += 0.75
        if atom.section == "experience":
            rel += 0.15
        return rel

    def _best_size(self, atom: Atom, rel: float) -> str:
        # A pinned atom was pinned because it matters, so it starts at its
        # fullest form and is only shortened if the page genuinely demands it.
        # Sizing pins by job relevance is backwards: the whole point of a pin is
        # that it appears even when the job does not obviously ask for it.
        if atom.pinned:
            size = "long"
        else:
            size = "long" if rel > 0.8 else ("medium" if rel > 0.35 else "short")
        while size and not self.renderable(atom, size):
            size = {"long": "medium", "medium": "short", "short": ""}[size]
        return size

    def _cost(self, atom: Atom, size: str, seen_groups: set[str]) -> int:
        cost = 1 + (len(atom.text(size)) // 105)
        if atom.group not in seen_groups:
            cost += 2          # the role heading this bullet renders under
        return cost

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
        jd_tokens = set(re.findall(r"[a-z][a-z0-9+#.]{1,}", jd))

        scored = [(self.relevance(a, jd, jd_tokens, family), a) for a in self.atoms]
        scored.sort(key=lambda p: -p[0])

        chosen: list[tuple[Atom, str]] = []
        seen_groups: set[str] = set()
        per_section: dict[str, int] = {}
        per_group: dict[str, int] = {}
        claims_used: dict[str, str] = {}
        used = 0
        pinned_dropped: list[str] = []

        def try_add(atom: Atom, rel: float, *, force: bool) -> bool:
            nonlocal used
            limits = self.sections.get(atom.section, {})
            if not force:
                if per_section.get(atom.section, 0) >= int(limits.get("max_atoms", 6)):
                    return False
                if per_group.get(atom.group, 0) >= max_per_group:
                    return False
            size = self._best_size(atom, rel)
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
            seen_groups.add(atom.group)
            per_section[atom.section] = per_section.get(atom.section, 0) + 1
            per_group[atom.group] = per_group.get(atom.group, 0) + 1
            used += cost
            return True

        # phase 1 -- non-negotiables
        for rel, a in [(r, a) for r, a in scored if a.pinned]:
            if not try_add(a, rel, force=True):
                pinned_dropped.append(a.id)

        # phase 2 -- best of the rest
        for rel, a in scored:
            if a.pinned:
                continue
            try_add(a, rel, force=False)

        gaps = self._gaps(jd_tokens)
        coverage = len(chosen) / max(1, len(self.atoms))
        return Selection(chosen, round(coverage, 3), gaps, claims_used,
                         dropped=len(self.atoms) - len(chosen),
                         pinned_dropped=pinned_dropped)

    def _gaps(self, jd_tokens: set[str]) -> list[str]:
        have = {t for a in self.atoms for t in a.tags}
        have |= {s.lower() for group in self.skills.values() for s in group}
        interesting = {
            "kubernetes", "terraform", "rust", "go", "golang", "scala", "kafka",
            "spark", "airflow", "graphql", "redis", "aws", "gcp", "azure",
            "tensorflow", "jax", "cuda", "triton", "vllm", "ray", "mlops",
            "kotlin", "swift", "ruby", "php", "elixir", "haskell", "matlab",
            "pentest", "fuzzing", "reverse", "malware", "siem", "soc",
        }
        return sorted((jd_tokens & interesting) - have)


def job_family(job: dict) -> str:
    """Which experience should lead, from the job text."""
    blob = f"{job.get('title','')} {job.get('description','')}".lower()
    if any(k in blob for k in ("security", "appsec", "infosec", "penetration",
                               "vulnerability", "red team", "threat")):
        return "security"
    if any(k in blob for k in ("quant", "trading", "trader", "market making",
                               "systematic", "derivative")):
        return "quant"
    if any(k in blob for k in ("machine learning", "ml engineer", " ai ", "deep learning",
                               "llm", "nlp", "research scientist", "computer vision")):
        return "ai_ml"
    if any(k in blob for k in ("graphics", "rendering", "unreal", "unity", "game",
                               "shader", "3d", "vfx")):
        return "graphics"
    if any(k in blob for k in ("research assistant", "phd", "publication", "laboratory")):
        return "research"
    return "default"
