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

    def text(self, size: str = "medium") -> str:
        return self.phrasings.get(size) or self.phrasings.get("medium") or ""


@dataclass
class Selection:
    atoms: list[tuple[Atom, str]]          # (atom, size)
    score: float
    gaps: list[str]                        # JD keywords with no matching atom
    claims_used: dict[str, str]


class Bank:
    def __init__(self, bank_dir: Path) -> None:
        self.dir = Path(bank_dir)
        self.atoms: list[Atom] = []
        self.sections: dict[str, dict] = {}
        self.skills: dict[str, list[str]] = {}
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
        for raw in adata.get("atoms", []):
            self.atoms.append(Atom(
                id=raw["id"], section=raw.get("section", "experience"),
                group=raw.get("group", raw["id"]), tags=raw.get("tags", []) or [],
                lead_for=raw.get("lead_for", []) or [],
                phrasings=raw.get("phrasings", {}) or {},
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
                    if tok and not re.fullmatch(r"\d{4}", tok):   # allow bare years
                        problems.append(
                            f"{a.id}.{size}: bare numeral {tok!r} — move it into claims.yaml")
                try:
                    self.resolve(text, strict=True)
                except BankError as e:
                    problems.append(f"{a.id}.{size}: {e}")
        return problems

    # ------------------------------------------------------------------ #
    def select(self, jd_text: str, *, family: str = "default",
               line_budget: int = 44) -> Selection:
        """Greedy knapsack: highest relevance per line, inside section limits."""
        jd = (jd_text or "").lower()
        jd_tokens = set(re.findall(r"[a-z][a-z0-9+#.]{1,}", jd))

        scored: list[tuple[float, Atom]] = []
        for a in self.atoms:
            hits = sum(1 for t in a.tags if t.replace("_", " ") in jd or t in jd_tokens)
            rel = hits / max(2.0, len(a.tags) or 1)
            if family in a.lead_for:
                rel += 0.75                      # lead atoms float to the top
            if a.section == "experience":
                rel += 0.15                      # real work outranks side projects
            scored.append((rel, a))
        scored.sort(key=lambda p: -p[0])

        per_section: dict[str, int] = {}
        chosen: list[tuple[Atom, str]] = []
        used_lines = 0
        claims_used: dict[str, str] = {}

        for rel, a in scored:
            limits = self.sections.get(a.section, {})
            cap = int(limits.get("max_atoms", 6))
            if per_section.get(a.section, 0) >= cap:
                continue
            size = "long" if rel > 0.8 else ("medium" if rel > 0.35 else "short")
            while size and not self.renderable(a, size):
                size = {"long": "medium", "medium": "short", "short": ""}[size]
            if not size:
                continue                          # every phrasing blocked by a retired claim
            cost = 1 + (len(a.text(size)) // 105)
            if used_lines + cost > line_budget:
                continue
            text, used = self.resolve(a.text(size))
            claims_used.update(used)
            chosen.append((a, size))
            per_section[a.section] = per_section.get(a.section, 0) + 1
            used_lines += cost

        # honesty: JD terms with no backing atom go to the gap report, never
        # into the document.
        gaps = self._gaps(jd_tokens)
        fill = sum(1 for _ in chosen) / max(1, len(self.atoms))
        return Selection(chosen, round(fill, 3), gaps, claims_used)

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
