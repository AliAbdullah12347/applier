"""Provider-agnostic LLM routing.

Swap engines by changing three lines in settings.yaml:

    llm:
      primary:
        provider: "openai"
        model: "gpt-5"
        api_key_env: "OPENAI_API_KEY"

Everything downstream keeps working. Fallbacks are tried in order when the
primary fails on a rate limit, quota exhaustion, or outage.

Three behaviours worth knowing about:

* **Serialised by default.** Free tiers punish parallelism (Mistral free is
  1 req/s). The router holds a semaphore rather than letting callers hammer.
* **PII redaction.** Some providers state that human reviewers may read free-tier
  inputs. When `llm.redact_pii` is true, identity is stripped before the call and
  restored locally afterwards, so only the job description and the bullet bank
  ever leave the machine.
* **Stable prefixes.** Prompts are assembled fixed-part-first so providers can
  cache them. Cached tokens usually do not count against rate limits.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..config import Config, get_secret

# --------------------------------------------------------------------------- #


class LLMError(RuntimeError):
    """Base class. `retryable` tells the router whether to fall through."""

    def __init__(self, msg: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(msg)
        self.retryable = retryable
        self.status = status


class RateLimited(LLMError):
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg, retryable=True, status=429)
        self.retry_after = retry_after


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)

    def json(self) -> Any:
        """Parse the reply as JSON, tolerating fenced code blocks and prose."""
        return parse_json_loose(self.text)



def _escape_raw_control_chars(text: str) -> str:
    """Escape newlines/tabs that appear *inside* JSON string literals.

    Models routinely return multi-paragraph prose inside a JSON string with
    literal newlines, which is invalid JSON. This walks the text tracking
    whether we are inside a quoted string, so only the control characters that
    actually break parsing are touched and formatting between tokens is left
    alone.
    """
    out: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
            elif ch == '\\':
                out.append(ch)
                esc = True
            elif ch == '"':
                in_str = False
                out.append(ch)
            elif ch == '\n':
                out.append('\\n')
            elif ch == '\r':
                out.append('\\r')
            elif ch == '\t':
                out.append('\\t')
            else:
                out.append(ch)
            continue
        if ch == '"':
            in_str = True
        out.append(ch)
    return ''.join(out)


def parse_json_loose(text: str) -> Any:
    """Models wrap JSON in prose and fences no matter how firmly you ask."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass

    # Models routinely emit multi-paragraph prose inside a JSON string with
    # LITERAL newlines, which is invalid JSON. That is not an edge case -- any
    # cover letter or long free-text answer hits it -- so repair it rather than
    # failing the whole generation.
    try:
        return json.loads(_escape_raw_control_chars(t))
    except json.JSONDecodeError:
        pass

    # Grab the outermost balanced {...} or [...]
    # Scan the REPAIRED text: scanning the raw text makes the object branch fail
    # on an unescaped newline and then silently succeed on an inner array,
    # returning the wrong value instead of an error.
    t = _escape_raw_control_chars(t)
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise LLMError(f"Could not parse JSON from model output: {text[:400]!r}")


# --------------------------------------------------------------------------- #
# PII redaction
# --------------------------------------------------------------------------- #
class Redactor:
    """Replace identity with stable placeholders, then restore them locally.

    Placeholders are stable within a call so the model can still refer to
    "the candidate" coherently.
    """

    def __init__(self, profile: Config | None, fields: list[str]) -> None:
        self.map: dict[str, str] = {}
        if not profile:
            return
        lookup = {
            "full_name": "identity.full_name",
            "first_name": "identity.first_name",
            "last_name": "identity.last_name",
            "phone": "identity.phone",
            "email": "identity.email",
            "linkedin": "identity.linkedin",
            "github": "identity.github",
            "address": "address.line1",
            "gpa": "education[0].gpa",
            "date_of_birth": "identity.date_of_birth",
            "visa_status": "work_authorization.visa_status",
        }
        for f in fields:
            dotted = lookup.get(f)
            if not dotted or "[" in dotted:
                continue
            val = profile.get(dotted, None)
            if isinstance(val, str) and len(val.strip()) > 2 and val.strip().upper() != "ASK":
                self.map[val.strip()] = f"<{f.upper()}>"
        # education gpa lives in a list
        try:
            gpa = (profile.get("education", []) or [{}])[0].get("gpa")
            if gpa and str(gpa).upper() != "ASK" and "gpa" in fields:
                self.map[str(gpa)] = "<GPA>"
        except Exception:
            pass

    def scrub(self, text: str) -> str:
        for real, token in sorted(self.map.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(real, token)
        return text

    def restore(self, text: str) -> str:
        for real, token in self.map.items():
            text = text.replace(token, real)
        return text


# --------------------------------------------------------------------------- #
# providers
# --------------------------------------------------------------------------- #
def _post(url: str, *, headers: dict, payload: dict, timeout: float) -> dict:
    try:
        r = httpx.post(url, headers=headers, json=payload, timeout=timeout)
    except httpx.TimeoutException as e:
        raise LLMError(f"timeout: {e}", retryable=True) from e
    except httpx.HTTPError as e:
        raise LLMError(f"transport: {e}", retryable=True) from e

    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        raise RateLimited(f"rate limited: {r.text[:200]}", float(ra) if ra and ra.isdigit() else None)
    if r.status_code in (500, 502, 503, 504, 529):
        raise LLMError(f"server {r.status_code}: {r.text[:200]}", retryable=True, status=r.status_code)
    if r.status_code >= 400:
        raise LLMError(f"http {r.status_code}: {r.text[:400]}", retryable=False, status=r.status_code)
    return r.json()


def _call_gemini(spec: dict, system: str, user: str, key: str, want_json: bool) -> LLMResponse:
    model = spec["model"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen: dict[str, Any] = {
        "temperature": spec.get("temperature", 0.3),
        "maxOutputTokens": spec.get("max_output_tokens", 4096),
    }
    if want_json:
        gen["responseMimeType"] = "application/json"

    # Gemini 3.x spends "thinking" tokens that are billed against maxOutputTokens
    # but never appear in the reply. On a long prompt they can consume most of
    # the budget and the actual answer comes back truncated mid-sentence -- which
    # surfaces downstream as a confusing JSON parse error rather than as the
    # capacity problem it is. Cap the thinking so the budget goes to the answer.
    budget = spec.get("thinking_budget")
    if budget is not None:
        gen["thinkingConfig"] = {"thinkingBudget": int(budget)}

    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": gen,
    }
    data = _post(url, headers={"x-goog-api-key": key}, payload=payload,
                 timeout=spec.get("timeout_s", 90))
    try:
        cand = data["candidates"][0]
        parts = cand["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError) as e:
        fb = data.get("promptFeedback", {})
        raise LLMError(f"gemini: no content ({fb or data})", retryable=False) from e

    um = data.get("usageMetadata", {})
    # Truncation is retryable and worth naming precisely: the caller otherwise
    # sees "could not parse JSON" and goes looking for a parser bug.
    if cand.get("finishReason") == "MAX_TOKENS":
        raise LLMError(
            f"gemini: output truncated at maxOutputTokens="
            f"{gen['maxOutputTokens']} (thinking used {um.get('thoughtsTokenCount', 0)} "
            f"of it). Raise llm.primary.max_output_tokens or lower thinking_budget.",
            retryable=True,
        )

    return LLMResponse(text, "gemini", model,
                       um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0), data)


def _call_openai_compatible(spec: dict, system: str, user: str, key: str,
                            want_json: bool, base_url: str, provider: str) -> LLMResponse:
    model = spec["model"]
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": spec.get("temperature", 0.3),
        "max_tokens": spec.get("max_output_tokens", 4096),
    }
    if want_json:
        payload["response_format"] = {"type": "json_object"}
    data = _post(f"{base_url}/chat/completions",
                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                 payload=payload, timeout=spec.get("timeout_s", 90))
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise LLMError(f"{provider}: no content ({str(data)[:200]})", retryable=False) from e
    u = data.get("usage", {})
    return LLMResponse(text, provider, model,
                       u.get("prompt_tokens", 0), u.get("completion_tokens", 0), data)


def _call_anthropic(spec: dict, system: str, user: str, key: str, want_json: bool) -> LLMResponse:
    model = spec["model"]
    if want_json:
        user += "\n\nRespond with a single valid JSON object and nothing else."
    payload = {
        "model": model,
        "max_tokens": spec.get("max_output_tokens", 4096),
        "temperature": spec.get("temperature", 0.3),
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    data = _post("https://api.anthropic.com/v1/messages",
                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                          "Content-Type": "application/json"},
                 payload=payload, timeout=spec.get("timeout_s", 90))
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage", {})
    return LLMResponse(text, "anthropic", model,
                       u.get("input_tokens", 0), u.get("output_tokens", 0), data)


def _call_ollama(spec: dict, system: str, user: str, _key: str, want_json: bool) -> LLMResponse:
    """Local models. Note: needs ~8GB RAM + 10GB disk free; check before enabling."""
    payload: dict[str, Any] = {
        "model": spec["model"],
        "prompt": user,
        "system": system,
        "stream": False,
        "options": {"temperature": spec.get("temperature", 0.3)},
    }
    if want_json:
        payload["format"] = "json"
    host = spec.get("host", "http://localhost:11434")
    data = _post(f"{host}/api/generate", headers={}, payload=payload,
                 timeout=spec.get("timeout_s", 300))
    return LLMResponse(data.get("response", ""), "ollama", spec["model"],
                       data.get("prompt_eval_count", 0), data.get("eval_count", 0), data)


PROVIDERS: dict[str, Callable[..., LLMResponse]] = {
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
    "ollama": _call_ollama,
    "openai": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.openai.com/v1", "openai"),
    "openrouter": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://openrouter.ai/api/v1", "openrouter"),
    "groq": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.groq.com/openai/v1", "groq"),
    "mistral": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.mistral.ai/v1", "mistral"),
    "together": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.together.xyz/v1", "together"),
    "cerebras": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.cerebras.ai/v1", "cerebras"),
    "deepseek": lambda s, sy, u, k, j: _call_openai_compatible(
        s, sy, u, k, j, "https://api.deepseek.com/v1", "deepseek"),
}


# --------------------------------------------------------------------------- #
# the router
# --------------------------------------------------------------------------- #
class Router:
    def __init__(self, settings: Config, profile: Config | None = None, db=None) -> None:
        self.s = settings
        self.db = db
        self.redactor = (
            Redactor(profile, settings.get("llm.redact_fields", []))
            if settings.get("llm.redact_pii", True) else None
        )
        self._sem = threading.Semaphore(max(1, int(settings.get("llm.rate_limit.max_concurrent", 1))))
        self._lock = threading.Lock()
        self._last_call = 0.0
        rpm = max(1, int(settings.get("llm.rate_limit.requests_per_minute", 14)))
        self._min_gap = 60.0 / rpm

    # ---------------------------------------------------------------- #
    def _chain(self, task: str | None) -> list[dict]:
        primary = dict(self.s.get("llm.primary"))
        override = self.s.get(f"llm.task_models.{task}", "primary") if task else "primary"
        if isinstance(override, dict):
            primary = {**primary, **override}
        elif isinstance(override, str) and override != "primary":
            primary = {**primary, "model": override}
        return [primary] + list(self.s.get("llm.fallbacks", []) or [])

    def _throttle(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < self._min_gap:
                time.sleep(self._min_gap - gap)
            self._last_call = time.monotonic()

    def _record(self, spec: dict, task: str | None, resp: LLMResponse | None, err: str | None) -> None:
        if not self.db:
            return
        try:
            from ..db import now
            self.db.run(
                "INSERT INTO llm_usage(at,provider,model,task,prompt_tokens,output_tokens,ok,error)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (now(), spec.get("provider", "?"), spec.get("model", "?"), task,
                 resp.prompt_tokens if resp else 0, resp.output_tokens if resp else 0,
                 1 if resp else 0, err),
            )
        except Exception:
            pass

    # ---------------------------------------------------------------- #
    def complete(
        self,
        system: str,
        user: str,
        *,
        task: str | None = None,
        want_json: bool = False,
        redact: bool | None = None,
    ) -> LLMResponse:
        """Run a completion through the chain. Raises LLMError if all links fail."""
        do_redact = self.redactor is not None if redact is None else (redact and self.redactor)
        if do_redact and self.redactor:
            system = self.redactor.scrub(system)
            user = self.redactor.scrub(user)

        attempts = int(self.s.get("llm.rate_limit.retry_attempts", 4))
        backoff = list(self.s.get("llm.rate_limit.retry_backoff_s", [2, 8, 30, 90]))
        errors: list[str] = []

        for spec in self._chain(task):
            provider = spec.get("provider")
            fn = PROVIDERS.get(provider)
            if not fn:
                errors.append(f"{provider}: unknown provider")
                continue
            key = ""
            if provider != "ollama":
                key = get_secret(spec.get("api_key_env", ""), required=False) or ""
                if not key:
                    errors.append(f"{provider}: no API key ({spec.get('api_key_env')})")
                    continue

            for attempt in range(attempts):
                try:
                    self._throttle()
                    with self._sem:
                        resp = fn(spec, system, user, key, want_json)
                    if do_redact and self.redactor:
                        resp.text = self.redactor.restore(resp.text)
                    self._record(spec, task, resp, None)
                    return resp
                except RateLimited as e:
                    wait = e.retry_after or backoff[min(attempt, len(backoff) - 1)]
                    errors.append(f"{provider}: 429 (waited {wait}s)")
                    self._record(spec, task, None, str(e))
                    if attempt < attempts - 1:
                        time.sleep(wait)
                except LLMError as e:
                    errors.append(f"{provider}: {e}")
                    self._record(spec, task, None, str(e))
                    if not e.retryable:
                        break
                    if attempt < attempts - 1:
                        time.sleep(backoff[min(attempt, len(backoff) - 1)])

        raise LLMError("All LLM providers failed:\n  " + "\n  ".join(errors))

    def json(self, system: str, user: str, *, task: str | None = None, **kw) -> Any:
        return self.complete(system, user, task=task, want_json=True, **kw).json()

    def health(self) -> list[dict]:
        """Which configured providers actually have a usable key."""
        out = []
        for spec in self._chain(None):
            p = spec.get("provider")
            has = p == "ollama" or bool(get_secret(spec.get("api_key_env", ""), required=False))
            out.append({"provider": p, "model": spec.get("model"),
                        "key_env": spec.get("api_key_env"), "ready": has})
        return out
