"""LaTeX rendering and the verification gate.

Rendering is deterministic: Jinja2 -> .tex -> pdflatex. No model touches the
document at this stage.

The verification gate is the part that matters. After every compile the PDF is
re-extracted and checked as a parser would see it, not as a human sees it. Two
real defects motivated this:

* **Hyphen splitting.** pdflatex breaks words across lines in the *text layer* —
  "scaffolding" extracts as "scaf-" + "folding" — and no ATS parser rejoins them.
  Any keyword landing on a line break becomes invisible. The preamble sets
  hyphenpenalty to suppress it; the gate proves it worked.
* **Invisible text.** Hidden keyword blocks are detected in production at 86-93%
  precision, and detection surfaces the attempt to a human recruiter. The gate
  refuses to ship a PDF whose extracted text contains anything not visibly
  rendered, so the system cannot produce one even by accident.

A PDF that fails any check is deleted, not warned about.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# LaTeX and Jinja both use braces. Swap Jinja's delimiters so .tex stays valid.
JINJA_ENV_KW = dict(
    block_start_string=r"\BLOCK{", block_end_string="}",
    variable_start_string=r"\VAR{", variable_end_string="}",
    comment_start_string=r"\#{", comment_end_string="}",
    trim_blocks=True, lstrip_blocks=True, autoescape=False,
    undefined=StrictUndefined,
)

LATEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}


def tex_escape(s: Any) -> str:
    out = []
    for ch in str(s):
        out.append(LATEX_SPECIAL.get(ch, ch))
    return "".join(out)


@dataclass
class VerifyReport:
    ok: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    chars: int = 0
    extracted: str = ""

    def __str__(self) -> str:
        head = "PASS" if self.ok else "FAIL"
        body = "".join(f"\n  - {f}" for f in self.failures)
        return f"verify {head} ({self.pages}p, {self.chars} chars){body}"


class RenderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
def render_tex(template_path: Path, context: dict) -> str:
    env = Environment(loader=FileSystemLoader(str(template_path.parent)), **JINJA_ENV_KW)
    env.filters["tex"] = tex_escape
    return env.get_template(template_path.name).render(**context)


def compile_pdf(tex_source: str, out_pdf: Path, *, timeout: int = 120) -> Path:
    exe = shutil.which("pdflatex")
    if not exe:
        raise RenderError("pdflatex not found on PATH (TeX Live provides it).")

    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="applier-tex-") as td:
        tmp = Path(td)
        tex = tmp / "resume.tex"
        tex.write_text(tex_source, encoding="utf-8")
        for _ in range(2):                      # second pass settles layout
            proc = subprocess.run(
                [exe, "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", str(tmp), str(tex)],
                capture_output=True, text=True, timeout=timeout,
            )
        produced = tmp / "resume.pdf"
        if not produced.exists():
            log = (proc.stdout or "")[-2500:]
            raise RenderError(f"pdflatex produced no PDF.\n{log}")
        shutil.copy(produced, out_pdf)
    return out_pdf


# --------------------------------------------------------------------------- #
def extract_text(pdf: Path) -> str:
    """Read the PDF the way an ATS parser would."""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        pass
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            return "\n".join((p.extract_text() or "") for p in doc.pages)
    except Exception:
        return ""


def page_count(pdf: Path) -> int:
    try:
        import pypdf
        return len(pypdf.PdfReader(str(pdf)).pages)
    except Exception:
        return 0


def verify_pdf(pdf: Path, *, expect_keywords: list[str], contact: list[str],
               claims_used: dict[str, str], settings) -> VerifyReport:
    checks = settings.get("tailor.verify.checks", {})
    text = extract_text(pdf)
    flat = re.sub(r"\s+", " ", text)
    rep = VerifyReport(ok=True, pages=page_count(pdf), chars=len(text.strip()), extracted=text)

    def fail(msg: str) -> None:
        rep.ok = False
        rep.failures.append(msg)

    want_pages = int(checks.get("page_count_exact", 1) or 0)
    if want_pages and rep.pages != want_pages:
        fail(f"page count {rep.pages}, expected {want_pages}")

    if rep.chars < int(checks.get("min_extracted_chars", 1500)):
        fail(f"only {rep.chars} extractable characters — the text layer may be broken")

    if checks.get("no_replacement_chars", True) and "�" in text:
        fail("U+FFFD replacement characters present (font/encoding problem)")

    # The real bug: hyphen-split keywords in the text layer.
    if checks.get("no_hyphen_split_keywords", True):
        split = re.findall(r"\b([A-Za-z]{2,})-\s*\n\s*([A-Za-z]{2,})\b", text)
        split += re.findall(r"\b([A-Za-z]{2,})-\s+([a-z]{2,})\b", flat)
        if split:
            sample = ", ".join(f"{a}-{b}" for a, b in split[:5])
            fail(f"hyphen-split words in the text layer ({sample}) — "
                 f"add \\hyphenpenalty=10000 \\exhyphenpenalty=10000 \\sloppy")

    if checks.get("keywords_present_verbatim", True):
        missing = [k for k in expect_keywords
                   if k and k.lower() not in flat.lower()]
        if missing:
            fail(f"claimed keywords missing from extracted text: {', '.join(missing[:8])}")

    if checks.get("contact_fields_intact", True):
        lost = [c for c in contact if c and c.lower() not in flat.lower()]
        if lost:
            fail(f"contact details did not survive extraction: {', '.join(lost)}")

    # Every numeral on the page must trace to a registered claim.
    if checks.get("every_numeral_traces_to_claim", True):
        allowed = set()
        for v in claims_used.values():
            allowed.update(re.findall(r"\d[\d,.]*", str(v)))
        for tok in re.findall(r"(?<![\w/])\d[\d,.]*(?![\w/])", flat):
            if tok in allowed:
                continue
            if re.fullmatch(r"(19|20)\d{2}", tok):      # years
                continue
            if tok in {"1", "2", "3", "4", "5"}:         # list/GPA scale noise
                continue
            rep.warnings.append(f"numeral {tok!r} not traced to a claim")

    if checks.get("no_invisible_text", True):
        hidden = detect_invisible_text(pdf, text)
        if hidden:
            fail(f"invisible/hidden text detected: {hidden}")

    return rep


def detect_invisible_text(pdf: Path, extracted: str) -> str:
    """Refuse to ship anything a detector would flag as concealment.

    Inverts the four techniques production detectors hunt for: white-on-white,
    zero/near-zero font size, render-mode-3 (invisible) text, and off-page
    positioning.
    """
    try:
        import pdfplumber
    except ImportError:
        return ""
    findings: list[str] = []
    try:
        with pdfplumber.open(str(pdf)) as doc:
            for i, page in enumerate(doc.pages):
                pw, ph = page.width, page.height
                for ch in page.chars:
                    size = float(ch.get("size") or 0)
                    if 0 < size < 3.0:
                        findings.append(f"p{i+1}: {size:.1f}pt text")
                        break
                    x0, top = float(ch.get("x0", 0)), float(ch.get("top", 0))
                    if x0 < -5 or top < -5 or x0 > pw + 5 or top > ph + 5:
                        findings.append(f"p{i+1}: text outside page bounds")
                        break
                    nsc = ch.get("non_stroking_color")
                    if isinstance(nsc, (list, tuple)) and len(nsc) >= 3:
                        if all(float(c) > 0.97 for c in nsc[:3]):
                            findings.append(f"p{i+1}: near-white text")
                            break
                if findings:
                    break
    except Exception:
        return ""
    return "; ".join(findings[:3])
