"""Per-ATS fast paths.

A known layout is faster and more reliable than inference, so when the platform
is recognised its adapter runs first. Anything an adapter does not handle falls
through to `universal.py`, which is why there is no such thing as an unsupported
form — the adapters are an optimisation, not the capability.

Each adapter contributes three things:

    selectors   where the resume/cover-letter file inputs actually are
    quirks      per-platform behaviour the generic path gets wrong
    advance     how to move to the next step on this platform

Adapters never click submit. That decision lives in the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Adapter:
    name: str
    #: CSS selectors for the resume file input, in priority order.
    resume_inputs: list[str] = field(default_factory=list)
    cover_inputs: list[str] = field(default_factory=list)
    #: Buttons that advance a multi-step flow, in priority order.
    advance_selectors: list[str] = field(default_factory=list)
    #: Selectors whose presence means the application was accepted.
    success_markers: list[str] = field(default_factory=list)
    #: True when each employer runs its own tenant needing its own account.
    per_tenant_accounts: bool = False
    #: Notes surfaced to the operator, not to a model.
    quirks: list[str] = field(default_factory=list)


GREENHOUSE = Adapter(
    name="greenhouse",
    resume_inputs=[
        'input[type=file]#resume',
        'input[type=file][name*=resume]',
        'input[type=file]',
    ],
    cover_inputs=['input[type=file]#cover_letter', 'input[type=file][name*=cover]'],
    advance_selectors=['button[type=submit]', 'input[type=submit]'],
    success_markers=['text="Application submitted"', '.application-confirmation'],
    quirks=[
        "Single-page form; usually one submit and done.",
        "Duplicate detection matches on email, phone and LinkedIn URL -- all constant "
        "for this candidate, so a second application to the same req is detected.",
        "Ships IPQS-backed fraud detection: datacenter IPs and timezone/location "
        "mismatches are fraud signals. Run headed, on a residential connection.",
    ],
)

LEVER = Adapter(
    name="lever",
    resume_inputs=['input[name=resume]', 'input[type=file]'],
    cover_inputs=['textarea[name*=cover]'],
    advance_selectors=['button[type=submit]', '.template-btn-submit'],
    success_markers=['text="Thank you"', '.application-confirmation'],
    quirks=[
        "Requirement lists are structurally separable: lists[].text headers "
        "distinguish 'What We Require' from 'What We Value', so hard vs nice-to-have "
        "can be parsed deterministically here rather than inferred.",
        "Cover letter is often a textarea, not a file upload.",
    ],
)

ASHBY = Adapter(
    name="ashby",
    resume_inputs=['input[type=file]', '[data-testid*=resume] input'],
    advance_selectors=['button[type=submit]', 'button:has-text("Submit Application")'],
    success_markers=['text="Thanks for applying"'],
    quirks=[
        "Heavily React-driven: values MUST be written through the native setter or "
        "the framework never sees them. The universal filler already does this.",
        "Custom questions are rendered dynamically; harvest after networkidle.",
    ],
)

WORKDAY = Adapter(
    name="workday",
    resume_inputs=[
        '[data-automation-id="file-upload-input-ref"]',
        'input[type=file]',
    ],
    advance_selectors=[
        '[data-automation-id="bottom-navigation-next-button"]',
        'button[data-automation-id*="next"]',
        'button[data-automation-id*="continue"]',
    ],
    success_markers=['[data-automation-id*="confirmation"]'],
    per_tenant_accounts=True,
    quirks=[
        "THE HARD ONE. Every employer runs a separate tenant, each needing its own "
        "account and email verification. Phase 1 (create account, click the "
        "verification link) is a one-time human task per company; phase 2 (everything "
        "after) is automated against the persistent browser profile.",
        "Elements are keyed on data-automation-id rather than id/name; prefer those.",
        "Multi-step wizard: expect 4-8 pages, not one form.",
        "Behind Akamai bot management that rate-limits by source IP ACROSS ALL "
        "TENANTS. One block takes out every Workday employer at once, which is why "
        "discovery never polls Workday directly.",
    ],
)

SMARTRECRUITERS = Adapter(
    name="smartrecruiters",
    resume_inputs=['input[type=file]'],
    advance_selectors=['button[type=submit]', 'button:has-text("Apply")'],
    quirks=["Often embeds a third-party consent step before the form proper."],
)

ICIMS = Adapter(
    name="icims",
    resume_inputs=['input[type=file]'],
    advance_selectors=['#quickApplyBtn', 'input[type=submit]', 'button[type=submit]'],
    quirks=[
        "Frequently renders the form inside an iframe -- harvest the frame, not the "
        "top document.",
        "Legacy markup: labels are often adjacent text nodes with no `for` attribute, "
        "which is exactly the case the universal filler's ancestor-walk handles.",
    ],
)

WORKABLE = Adapter(
    name="workable",
    resume_inputs=['input[type=file]'],
    advance_selectors=['button[type=submit]', 'button:has-text("Submit")'],
)

RECRUITEE = Adapter(
    name="recruitee",
    resume_inputs=['input[type=file]'],
    advance_selectors=['button[type=submit]'],
)

TEAMTAILOR = Adapter(
    name="teamtailor",
    resume_inputs=['input[type=file]'],
    advance_selectors=['button[type=submit]'],
)

BAMBOOHR = Adapter(
    name="bamboohr",
    resume_inputs=['input[type=file]'],
    advance_selectors=['button[type=submit]'],
)

#: Fallback used when the platform is unrecognised. Empty selectors mean the
#: universal filler supplies everything, which is the normal case for bespoke
#: careers pages.
GENERIC = Adapter(
    name="generic",
    resume_inputs=['input[type=file][name*=resume]', 'input[type=file][accept*=pdf]',
                   'input[type=file]'],
    advance_selectors=['button[type=submit]', 'input[type=submit]'],
    quirks=["Unrecognised platform: the universal filler handles this end to end."],
)


REGISTRY: dict[str, Adapter] = {
    a.name: a for a in (
        GREENHOUSE, LEVER, ASHBY, WORKDAY, SMARTRECRUITERS, ICIMS,
        WORKABLE, RECRUITEE, TEAMTAILOR, BAMBOOHR, GENERIC,
    )
}


def for_ats(ats: str | None) -> Adapter:
    """Look up an adapter, falling back to the generic one."""
    return REGISTRY.get((ats or "").lower(), GENERIC)


def needs_account(ats: str | None) -> bool:
    return for_ats(ats).per_tenant_accounts


__all__ = ["Adapter", "REGISTRY", "for_ats", "needs_account"]
