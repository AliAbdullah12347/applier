"""Measuring the model chain, because free-tier quotas move.

A fallback chain is only worth its order. Ali's ran

    gemini-3.5-flash  ->  gemini-3.8-flash  ->  flash-latest  ->  flash-lite

and the measurements were: 429 immediately, 75-second timeout, 503, then
1.7 seconds and a correct answer. Exactly backwards. Every call paid the
first three before reaching the one that worked, which is where preflight's
123 seconds came from.

Nothing about that was visible. The chain reported "gemini-3.5-flash
answered" afterwards because it eventually did, once its quota recovered,
and the three failures in between left no mark on screen.

So this measures each link and says what it found. It writes nothing: the
order is a decision, and `PUT /api/models` already replaces a chain
atomically. `suggest_order` is the recommendation, not the act.

Ordering principle
------------------
A model that answers goes first, fastest first. After that, failures are
ordered by how *cheap* they are to try, because every link is attempted in
turn and pays its own timeout before the next one starts. A model that
429s in under a second costs almost nothing to keep ahead of one that
burns seventy-five seconds before giving up.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Config, get_secret

# Per-link budget. Long enough that a slow-but-working model is not called
# broken, short enough that probing a four-link chain is not a coffee break.
PROBE_TIMEOUT_S = 20.0

PROMPT_SYSTEM = "Reply with exactly the word: ready."
PROMPT_USER = "Say ready."

# Worst to best; `rank` uses the position.
UNUSABLE = ("timeout", "error", "unavailable", "rate_limited", "no_key", "ok")


@dataclass
class Probe:
    model: str
    provider: str
    role: str                       # "primary" or "fallback"
    state: str                      # one of UNUSABLE
    seconds: float
    detail: str = ""
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    def as_dict(self) -> dict[str, Any]:
        return {"model": self.model, "provider": self.provider, "role": self.role,
                "state": self.state, "seconds": round(self.seconds, 2),
                "detail": self.detail[:200], "ok": self.ok}


def _one_line(text: str, limit: int = 120) -> str:
    """Providers answer errors with pretty-printed JSON. A table needs a line."""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _classify(exc: Exception) -> tuple[str, str]:
    """What kind of unusable, from an exception the router raised."""
    from .router import RateLimited

    text = _one_line(str(exc))
    # A provider answers an error with pretty-printed JSON, and forty
    # characters of it says "rate limited: { "error": { "code"". The state
    # already carries the meaning; the detail is for the part that varies.
    if isinstance(exc, RateLimited):
        return "rate_limited", "quota exceeded (429)"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return "timeout", "no response before the probe gave up"
    # 5xx is the provider having a bad minute, which is worth distinguishing
    # from a model name that does not exist.
    for code in ("500", "502", "503", "504"):
        if code in text:
            return "unavailable", f"provider returned {code}"
    if "overloaded" in low:
        return "unavailable", "provider reports it is overloaded"
    if "not found" in low or "404" in text:
        return "error", "no such model for this key"
    return "error", text


def probe_chain(settings: Config, profile: Config | None = None, *,
                timeout_s: float = PROBE_TIMEOUT_S) -> list[Probe]:
    """Call every link in the chain once. Writes nothing, changes nothing."""
    from .router import PROVIDERS, LLMError

    primary = dict(settings.get("llm.primary", {}) or {})
    fallbacks = [dict(f) for f in (settings.get("llm.fallbacks", []) or [])]

    out: list[Probe] = []
    for role, spec in [("primary", primary)] + [("fallback", f) for f in fallbacks]:
        provider = str(spec.get("provider") or "")
        model = str(spec.get("model") or "")
        fn = PROVIDERS.get(provider)
        if not fn:
            out.append(Probe(model, provider, role, "error", 0.0,
                             f"unknown provider {provider!r}", spec))
            continue

        key = ""
        if provider != "ollama":
            key = get_secret(str(spec.get("api_key_env") or ""), required=False) or ""
            if not key:
                out.append(Probe(model, provider, role, "no_key", 0.0,
                                 f"no key in {spec.get('api_key_env')}", spec))
                continue

        # The real spec, with only the timeout bounded. Probing with a
        # different token budget is how a thinking model reads as broken:
        # eight output tokens get spent on reasoning and the reply comes
        # back empty, which looks like the model's fault and is not.
        attempt = {**spec, "timeout_s": timeout_s}
        started = time.perf_counter()
        try:
            reply = fn(attempt, PROMPT_SYSTEM, PROMPT_USER, key, False)
        except LLMError as exc:
            state, detail = _classify(exc)
            out.append(Probe(model, provider, role, state,
                             time.perf_counter() - started, detail, spec))
        except Exception as exc:                        # noqa: BLE001
            out.append(Probe(model, provider, role, "error",
                             time.perf_counter() - started, _one_line(str(exc)), spec))
        else:
            took = time.perf_counter() - started
            text = (reply.text or "").strip()
            if "ready" not in text.lower():
                out.append(Probe(model, provider, role, "error", took,
                                 f"answered, but not usefully: {text[:60]!r}", spec))
            else:
                out.append(Probe(model, provider, role, "ok", took, "", spec))
    return out


def rank(p: Probe) -> tuple:
    """Sort key. Working first and fastest; then cheapest failure first."""
    if p.ok:
        return (0, p.seconds)
    # Among the unusable, prefer the ones that give up quickly -- the chain
    # pays each link's time before trying the next.
    return (1, p.seconds, UNUSABLE.index(p.state) if p.state in UNUSABLE else 0)


def suggest_order(probes: list[Probe]) -> list[Probe]:
    return sorted(probes, key=rank)


def would_change(probes: list[Probe]) -> bool:
    return [p.model for p in probes] != [p.model for p in suggest_order(probes)]


def as_chain(probes: list[Probe]) -> dict[str, Any]:
    """The suggested order, shaped for `PUT /api/models`."""
    order = suggest_order(probes)
    if not order:
        return {}
    return {"primary": dict(order[0].spec),
            "fallbacks": [dict(p.spec) for p in order[1:]]}
