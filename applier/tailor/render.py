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


def tex_url(s: Any) -> str:
    r"""A URL safe to put inside \href{...}.

    `tex_escape` is wrong here: it turns `~` into `\textasciitilde{}` and `_`
    into `\_`, and hyperref wants the characters themselves. Only the three
    that actually end an argument or start a comment need escaping, and a
    URL carrying a brace or a backslash is not one this program will link.
    """
    url = str(s or "").strip()
    # A brace closes the argument, a backslash starts a command, whitespace
    # splits it. None of those belong in a link this program renders, and a
    # URL carrying one is dropped rather than patched into something else.
    if any(ch in url for ch in "{}\\") or any(ch.isspace() for ch in url):
        return ""
    return url.replace("%", r"\%").replace("#", r"\#").replace("&", r"\&")


def link_label(url: str, given: str = "") -> str:
    """What the link says on the page.

    The original resume wrote "GitHub" and "Live Demo" rather than the bare
    address, which is shorter and reads better on a line that already has a
    name and a stack on it. An explicit `link_text:` in the master file wins;
    otherwise the host decides.
    """
    if given:
        return given
    host = re.sub(r"^https?://(www\.)?", "", str(url or "")).split("/")[0].lower()
    if "github.com" in host:
        return "GitHub"
    if "gitlab.com" in host:
        return "GitLab"
    if any(h in host for h in ("vercel.app", "netlify.app", "herokuapp.com",
                               "pages.dev", "streamlit.app")):
        return "Live Demo"
    return host or "Link"


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
    env.filters["texurl"] = tex_url
    env.globals["link_label"] = link_label
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
    """Read the PDF the way an ATS parser would.

    pdfplumber first, pypdf only as a fallback. The order used to be the other
    way round, and pypdf infers word spacing poorly across a two-column
    `tabular*` row: a job title and its dates came back joined as
    "Computational AlgebraMay 2026". poppler and pdfminer both read the same
    file correctly, so the PDF was fine and the *verifier* had the distorted
    view — which is the worse failure, because this function is what decides
    whether a resume is fit to send. A gate that misreads the artifact it is
    inspecting can fail a good file or pass a broken one.

    pdfplumber is built on pdfminer, which is what several real ATS pipelines
    use, so this is also closer to the thing being simulated.
    """
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as doc:
            text = "\n".join((p.extract_text() or "") for p in doc.pages)
        if text.strip():
            return text
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        return ""


def squeezed_words(pdf: Path) -> list[str]:
    """Words a parser will run together because the spaces between them are too thin.

    LaTeX justifies a line by shrinking interword glue. Push enough text onto
    one line and the spaces narrow until they are no longer distinguishable
    from ordinary inter-letter spacing -- at which point extraction silently
    joins the words. A real skills line came out of pdfplumber as

        SageMath,Galoisfieldarithmetic,finiteprojectivegeometry,linearalgebra

    on a PDF that *looks* perfect, because the spaces were still there, just
    2.9pt wide against pdfplumber's 3pt word-split threshold.

    This is not merely a verifier blind spot like the pypdf column collision
    was. pdfminer -- which pdfplumber is built on -- is what several real ATS
    pipelines use, so a line this tight genuinely loses its word boundaries on
    the employer's side. The skills section is the part of a resume most likely
    to be keyword-matched, which makes it the worst possible place to lose them.

    Measured rather than guessed: extract twice, once at the default tolerance
    and once at a tolerance tight enough that only true letter spacing merges.
    Anything that is one token at the default and several when read tightly has
    spaces in the ambiguous band, and is reported.
    """
    try:
        import pdfplumber
    except Exception:
        return []

    merged: list[str] = []
    try:
        with pdfplumber.open(str(pdf)) as doc:
            for page in doc.pages:
                loose = {w["text"] for w in page.extract_words(x_tolerance=3.0)}
                tight = {w["text"] for w in page.extract_words(x_tolerance=1.2)}
                for word in loose - tight:
                    # A comma or a full stop inside a token is conclusive: no
                    # word contains one, so the gap after it was a real space.
                    # Otherwise require some length, so that a tight tolerance
                    # splitting a kerned pair does not raise a false alarm.
                    if re.search(r"[,.;]\w", word) or len(word) >= 16:
                        merged.append(word)
    except Exception:
        return []
    return sorted(merged, key=len, reverse=True)


def page_count(pdf: Path) -> int:
    try:
        import pypdf
        return len(pypdf.PdfReader(str(pdf)).pages)
    except Exception:
        return 0


def verify_pdf(pdf: Path, *, expect_keywords: list[str], contact: list[str],
               claims_used: dict[str, str], settings,
               profile_numerals: list[str] | None = None) -> VerifyReport:
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

    # The mirror image of a hyphen split: two columns whose text runs collide,
    # so a role title and its dates come out as one token. "Computational
    # AlgebraMay 2026" costs the parser the employment date, which is a field
    # most ATS index on. Checked against a month name or a year specifically,
    # because that is the collision that matters and it produces no false
    # positives on ordinary CamelCase like "PyTorch" or "JavaScript".
    if checks.get("no_collided_columns", True):
        months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
        collided = re.findall(rf"\b([A-Za-z]{{3,}}?)((?:{months})[a-z]*\s+(?:19|20)\d{{2}})", flat)
        collided += [(a, b) for a, b in re.findall(r"\b([a-z]{3,})((?:19|20)\d{2})\b", flat)]
        if collided:
            sample = ", ".join(f"{a}|{b}" for a, b in collided[:4])
            fail(f"column text runs collided in the text layer ({sample}) — a "
                 f"parser reads the heading and the date as one token and loses "
                 f"the date. Shorten the heading or widen the gap.")

    # Spaces too thin to survive extraction. Unlike the collision check above,
    # this one is not about the verifier's eyesight: the parser on the
    # employer's side uses the same family of heuristic, so a line squeezed
    # this hard really does arrive with its words run together.
    #
    # Reported as a failure rather than a warning because it is actionable and
    # self-correcting: the autofit ladder retries with a smaller budget, the
    # line stops being overfull, and the spaces come back.
    if checks.get("no_squeezed_words", True):
        squeezed = squeezed_words(pdf)
        if squeezed:
            sample = ", ".join(squeezed[:3])
            fail(f"interword spaces too narrow to survive extraction "
                 f"({sample}) -- the line is being justified so tightly that a "
                 f"parser reads the words as one token. Shorten it.")

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
    # Contact details and the claim values themselves are legitimate sources, so
    # both are tokenised into the allowed set -- otherwise a phone number and a
    # "$5,000" split by the tokeniser produce warnings that train you to ignore
    # the check entirely, which defeats its purpose.
    if checks.get("every_numeral_traces_to_claim", True):
        # Profile-sourced figures -- GPA, its scale, the graduation date -- are
        # typeset from config/profile.yaml, not from the claims ledger, so they
        # have no claim to trace to. Flagging them produced a permanent warning
        # on every single resume ("numeral '00' not traced", from the GPA line),
        # and a check that always warns is a check people stop reading.
        allowed: set[str] = set()
        for v in (list(claims_used.values()) + list(contact)
                  + list(profile_numerals or [])):
            raw = str(v)
            allowed.update(re.findall(r"\d[\d,.]*", raw))
            allowed.update(re.findall(r"\d+", raw))          # sub-tokens: 5,000 -> 5, 000
            allowed.add(re.sub(r"[^\d]", "", raw))            # digits-only form
        from .master import is_literal_numeral

        for m in re.finditer(r"(?<![\w/])\d[\d,.]*(?![\w/])", flat):
            raw = m.group(0)
            # Third place this bit me: `[\d,.]*` takes the sentence's comma or
            # full stop with it, so "5," and "5.3," missed every exemption
            # below and warned on every render. A check that always warns is a
            # check nobody reads, which is worse than not having it.
            tok = raw.rstrip(".,;:")
            if not tok or tok in allowed:
                continue
            # The same rule the importer and lint use: years, trivial counts
            # and version numbers are legitimately literal.
            if is_literal_numeral(tok, flat[max(0, m.start() - 24):m.start()]):
                continue
            if re.sub(r"[^\d]", "", tok) in allowed:     # same digits, different punctuation
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
