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
from applier.tailor.bank import job_family                        # noqa: E402
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


def main() -> int:
    settings, profile = load_settings(), load_profile()
    out = ROOT / "out" / "families"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"{'family':9} {'resolved':9} {'pg':>2} {'ok':>5} {'sq':>3} {'skl':>3} "
          f"{'blt':>3}  sections")
    print("-" * 78)

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
        n_bullets = sum(1 for ln in lines if ln.startswith("–"))

        wrong = (not report.ok or report.pages != 1 or squeezed or fam != want)
        bad += bool(wrong)
        print(f"{want:9} {fam:9} {report.pages:>2} {str(report.ok):>5} "
              f"{len(squeezed):>3} {n_skills:>3} {n_bullets:>3}  "
              f"{' > '.join(s[:4] for s in order)}{'  <-- CHECK' if wrong else ''}")
        if report.warnings:
            print(f"{'':9} warnings: {report.warnings[:2]}")
        if squeezed:
            print(f"{'':9} runs together: {squeezed[0][:64]}")

    print("-" * 78)
    print("ALL CLEAN" if not bad else f"{bad} need attention")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
