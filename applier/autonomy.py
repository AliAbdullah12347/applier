"""How much the system is allowed to do without you.

Ali asked for one dial: "from fully involved to completely autonomous". The
underlying settings that control this were already there but scattered —
`apply.mode`, `search.min_score_to_autoapply`, `apply.max_per_hour` — and
tuning them individually is exactly the kind of configuration archaeology the
GUI is supposed to remove.

So there is a single named level, and it projects onto those settings. The
levels are deliberately coarse. A dial with eleven positions is one nobody
understands the middle of.

    involved     find and prepare. Never opens a form. You apply yourself.
    review       fill every field, then stop and hand you the browser.
    assisted     submit the confident ones, queue the rest for your approval.
    autonomous   submit everything that clears the gates.

What does NOT change with the level, at any setting:

  * Eligibility gates still run. "Autonomous" means unattended, not reckless.
  * A legal or immigration question it cannot resolve exactly still halts.
  * A visible CAPTCHA still pauses and waits for a human.
  * Per-employer caps and lockouts still hold.

Those are integrity rules, not preferences, so they are not on the dial.
"""

from __future__ import annotations

from dataclasses import dataclass

INVOLVED = "involved"
REVIEW = "review"
ASSISTED = "assisted"
AUTONOMOUS = "autonomous"

ORDER = [INVOLVED, REVIEW, ASSISTED, AUTONOMOUS]


@dataclass(frozen=True)
class Level:
    key: str
    rank: int
    label: str
    blurb: str
    apply_mode: str          # dry_run | review | auto
    min_score: float         # auto-apply threshold
    submits: bool


LEVELS: dict[str, Level] = {
    INVOLVED: Level(
        INVOLVED, 0, "Fully involved",
        "Finds jobs, scores them, tailors the resume and writes the cover letter. "
        "Stops there. Nothing is opened or sent — you apply yourself with the "
        "documents it prepared.",
        apply_mode="dry_run", min_score=0.60, submits=False),
    REVIEW: Level(
        REVIEW, 1, "Review each one",
        "Opens the application, fills every field, and stops with the browser on "
        "screen. You read it and press submit.",
        apply_mode="review", min_score=0.60, submits=False),
    ASSISTED: Level(
        ASSISTED, 2, "Assisted",
        "Submits strong matches on its own. Anything weaker, or anything it is "
        "unsure about, waits in the queue for your approval.",
        apply_mode="auto", min_score=0.75, submits=True),
    AUTONOMOUS: Level(
        AUTONOMOUS, 3, "Fully autonomous",
        "Submits everything that clears the eligibility gates. Still halts on "
        "legal questions it cannot answer exactly, and on CAPTCHAs.",
        apply_mode="auto", min_score=0.60, submits=True),
}


def current(settings) -> str:
    """The configured level, inferred from the underlying settings if unset.

    Inference matters for anyone who edited settings.yaml by hand before this
    dial existed: their file has an `apply.mode` and no `autonomy.level`, and
    silently resetting them to a default would change what the system does
    behind their back.
    """
    lvl = settings.get("autonomy.level", None)
    if lvl in LEVELS:
        return lvl

    mode = settings.get("apply.mode", "auto")
    if mode == "dry_run":
        return INVOLVED
    if mode == "review":
        return REVIEW

    # `mode: auto` alone does not distinguish "assisted" from "fully
    # autonomous" — the threshold is the only other signal, and a
    # hand-written 0.70 was never a deliberate vote for either one.
    #
    # So ambiguity resolves DOWNWARD. Reporting the higher level would put
    # "Fully autonomous" on screen for someone who never chose it, and the
    # difference between the two is whether this software sends applications
    # in a real person's name unattended. An under-claim costs one click; an
    # over-claim costs an application he did not agree to.
    return AUTONOMOUS if str(lvl or "").strip() == AUTONOMOUS else ASSISTED


def apply_level(settings, key: str) -> Level:
    """Project a level onto the settings object. Returns the level applied."""
    if key not in LEVELS:
        raise ValueError(f"unknown autonomy level {key!r}; expected one of {ORDER}")
    lvl = LEVELS[key]
    settings.set("autonomy.level", lvl.key)
    settings.set("apply.mode", lvl.apply_mode)
    settings.set("search.min_score_to_autoapply", lvl.min_score)
    return lvl


def describe() -> list[dict]:
    return [
        {"key": l.key, "rank": l.rank, "label": l.label, "blurb": l.blurb,
         "submits": l.submits, "apply_mode": l.apply_mode, "min_score": l.min_score}
        for l in sorted(LEVELS.values(), key=lambda x: x.rank)
    ]
