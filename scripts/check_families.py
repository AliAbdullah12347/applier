"""Render one resume per job family and report what came out.

    python scripts/check_families.py

Reading the selection code cannot tell you whether a resume is still one page,
still extracts cleanly, still leads with the right experience, and still fits
its skills block. Rendering one per family can, and it takes seconds.

This is the check that caught the skills block eating eleven lines, the
interword spaces sitting 0.07pt above pdfminer's word-split threshold, and a
client-service posting being handed a resume led by finite-geometry research.
None of them were visible in a diff.

The postings below are synthetic and deliberately plain. They are not meant to
be realistic listings -- they are the minimum text that lands a resume in each
family, so that what varies between runs is the bank and the renderer, not the
input.
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from applier.config import load_profile, load_settings           # noqa: E402
from applier.config import data_path                              # noqa: E402
from applier.tailor.bank import (                                  # noqa: E402
    WRAP_COLUMN, Bank, date_key, job_family)
from applier.tailor.pipeline import build_resume                  # noqa: E402
from applier.tailor.render import extract_text, squeezed_words    # noqa: E402

JOBS: list[tuple[str, str, str]] = [
    ("ai_ml", "Machine Learning Engineer Intern",
     "Join our LLM team. You will fine-tune transformer models in PyTorch, build "
     "retrieval-augmented generation pipelines, and evaluate model quality. "
     "Experience with Python, PyTorch, NumPy and prompt engineering required."),
    ("quant", "Quantitative Trading Intern",
     "Systematic trading desk. You will research alpha signals, build statistical "
     "models, and work on market making strategies. Strong mathematics, "
     "probability, linear algebra and Python required. Olympiad background valued."),
    ("security", "Security Engineering Intern",
     "Application security team. You will perform threat modelling, review code "
     "for vulnerabilities, run penetration tests and work on prompt-injection "
     "defence for our LLM products. Knowledge of the OWASP Top 10 required."),
    ("graphics", "Graphics Engine Intern",
     "Real-time rendering team. You will optimise shaders in HLSL, work on LOD "
     "pipelines in Unreal Engine 5, and profile frame time on consumer hardware. "
     "Blender and 3D asset pipelines a plus."),
    ("research", "Research Assistant, Computational Biology",
     "Laboratory position supporting peer-reviewed publication. You will analyse "
     "RNA-seq data, run differential expression and cell-type deconvolution, and "
     "contribute to a dissertation-track project. R, Python and statistics required."),
    ("client", "Summer Associate, Client Service",
     "You will manage end-to-end client-expert communications, screen experts by "
     "phone, negotiate terms, and coordinate across teams. Strong academic "
     "credentials and noteworthy extracurricular achievement required. "
     "Visa sponsorship is not offered for this role."),
    ("default", "Software Engineering Intern",
     "Build and ship backend services in Python and TypeScript. You will work on "
     "REST APIs, PostgreSQL, React front ends, and automated testing. We value "
     "engineers who have shipped something end to end on their own."),
]

SECTION_NAMES = ("Education", "Technical Skills", "Experience",
                 "Leadership", "Projects")




def _page_fill(pdf: Path) -> float:
    """Fraction of the printable page the content reaches, 0.0 - 1.0."""
    try:
        import pymupdf
    except ImportError:                                # noqa: BLE001
        return 0.0
    try:
        with pymupdf.open(pdf) as doc:
            page = doc[0]
            blocks = [b for b in page.get_text("blocks") if (b[4] or "").strip()]
            if not blocks:
                return 0.0
            top = min(b[1] for b in blocks)
            bottom = max(b[3] for b in blocks)
            return (bottom - top) / max(1.0, page.rect.height - 2 * top)
    except Exception:                                  # noqa: BLE001
        return 0.0


def main() -> int:
    settings, profile = load_settings(), load_profile()
    # Only for the date lines. The resumes themselves are built by
    # `build_resume`, which loads its own bank the way the real run does.
    roles = Bank(data_path(settings.get("tailor.bank_path", "config/bank"))).roles
    out = ROOT / "out" / "families"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"{'family':9} {'resolved':9} {'pg':>2} {'ok':>5} {'sq':>3} {'skl':>3} "
          f"{'blt':>3} {'wrap':>4} {'fill':>4} {'desc':>4}  leads with")
    print("-" * 92)

    bad = 0
    for want, title, desc in JOBS:
        job = {"id": 0, "title": title, "description": desc, "company": "Example"}
        fam = job_family(job)
        art = out / want
        art.mkdir(parents=True, exist_ok=True)
        try:
            pdf, report, _sel = build_resume(job, settings, profile, art)
        except Exception as exc:                       # noqa: BLE001
            print(f"{want:9} RENDER FAILED: {exc}")
            bad += 1
            continue

        text = extract_text(pdf)
        (art / "extracted.txt").write_text(text, encoding="utf-8")
        squeezed = squeezed_words(pdf)
        lines = [ln for ln in text.splitlines() if ln.strip()]

        n_skills = -1
        if "Technical Skills" in lines:
            buf: list[str] = []
            for ln in lines[lines.index("Technical Skills") + 1:]:
                if ln in SECTION_NAMES:
                    break
                buf.append(ln)
            n_skills = len(" ".join(buf).split(",")) if buf else 0

        order = [ln for ln in lines if ln in SECTION_NAMES]
        bullets = [ln for ln in lines if ln.startswith("–")]
        n_bullets = len(bullets)

        # A bullet past the column width wraps to a second line. Two-line
        # bullets are the difference between a page that is read and a page
        # that is skimmed past, so count them rather than eyeballing the PDF.
        n_wrap = sum(1 for b in bullets if len(b) > WRAP_COLUMN)

        # How much of the page the content actually uses. A resume that stops
        # two inches short looks unfinished; one that reaches the margin has
        # spent the space it was given.
        fill = _page_fill(pdf)

        # What a recruiter's eye lands on first: the heading under the first
        # section that carries entries. Leading a backend posting with finite
        # geometry is the specific failure this column exists to catch.
        lead = ""
        for idx, ln in enumerate(lines):
            if ln == "Experience" and idx + 1 < len(lines):
                lead = lines[idx + 1]
                break

        # Experience prints newest first, so a reader can check the dates
        # descend before reading a word. Taken from the selection rather than
        # the extracted text, because a date reached from a heading line is a
        # guess and this needs to be exact. A date the parser cannot read
        # sorts to the bottom, which shows up here as a run that stops
        # descending -- usually a typo in the master file.
        dated: list[tuple[str, tuple[int, int]]] = []
        for atom, _size in _sel.atoms:
            if atom.section != "experience":
                continue
            if dated and dated[-1][0] == atom.group:
                continue
            dated.append((atom.group,
                          date_key(str((roles.get(atom.group) or {}).get("dates", "")))))
        descends = all(a[1] >= b[1] for a, b in zip(dated, dated[1:]))

        wrong = (not report.ok or report.pages != 1 or squeezed or fam != want
                 or not descends)
        bad += bool(wrong)
        print(f"{want:9} {fam:9} {report.pages:>2} {str(report.ok):>5} "
              f"{len(squeezed):>3} {n_skills:>3} {n_bullets:>3} {n_wrap:>4} "
              f"{fill:>3.0%} {'yes' if descends else 'NO ':>4}  "
              f"{lead[:30]}{'  <-- CHECK' if wrong else ''}")
        if not descends:
            print(f"{'':9} out of order: "
                  + " | ".join(g for g, _ in dated))
        if report.warnings:
            print(f"{'':9} warnings: {report.warnings[:2]}")
        if squeezed:
            print(f"{'':9} runs together: {squeezed[0][:64]}")

    print("-" * 78)
    print("ALL CLEAN" if not bad else f"{bad} need attention")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
