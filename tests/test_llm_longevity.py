"""Outliving any particular vendor.

This system has to keep working for years, and the thing most likely to break
in that time is not the code -- it is a model being retired, an endpoint
moving, or the best provider in 2029 being one nobody has heard of today.

None of those should require editing Python. The rule these tests describe is
that `settings.yaml` can reach any vendor exposing an OpenAI-compatible
`/chat/completions`, which is nearly all of them, and can repoint the built-in
providers at a different base URL when one changes its path.

The calls here go to a real HTTP server on loopback rather than to a mock, so
what is tested is the request the router actually puts on the wire: its URL,
its headers, and its body.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from applier.config import Config
from applier.llm.router import PROVIDERS, LLMError, Router, _required_base_url


class _Handler(BaseHTTPRequestHandler):
    """A minimal stand-in for an OpenAI-compatible vendor."""

    received: list[dict] = []

    def do_POST(self) -> None:                      # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).received.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body,
        })
        payload = json.dumps({
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a) -> None:              # noqa: D102
        pass


@pytest.fixture
def vendor():
    """A throwaway vendor on loopback. Yields (base_url, received)."""
    _Handler.received = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/v1", _Handler.received
    finally:
        srv.shutdown()
        srv.server_close()


def _router(primary: dict, *, fallbacks=None, monkeypatch=None) -> Router:
    cfg = Config({
        "llm": {
            "primary": primary,
            "fallbacks": fallbacks or [],
            "redact_pii": False,
            "rate_limit": {"requests_per_minute": 6000, "retry_attempts": 1,
                           "retry_backoff_s": [0], "max_concurrent": 4},
        }
    })
    return Router(cfg, None, None)


@pytest.fixture(autouse=True)
def _fake_key(monkeypatch):
    """Every provider but ollama needs a key; none of these are real."""
    monkeypatch.setenv("TEST_LLM_KEY", "sk-not-a-real-key")


# --------------------------------------------------------------------------- #
# the escape hatch
# --------------------------------------------------------------------------- #
def test_a_provider_that_does_not_exist_yet_works_from_settings_alone(vendor):
    """The whole point: no code change to adopt a new vendor."""
    base_url, received = vendor
    r = _router({
        "provider": "openai_compatible",
        "base_url": base_url,
        "model": "some-model-from-2029",
        "api_key_env": "TEST_LLM_KEY",
    })
    out = r.complete("sys", "hello")

    assert out.text == "pong"
    assert len(received) == 1
    assert received[0]["path"] == "/v1/chat/completions"
    assert received[0]["auth"] == "Bearer sk-not-a-real-key"
    assert received[0]["body"]["model"] == "some-model-from-2029"


def test_a_built_in_provider_can_be_repointed_at_a_new_url(vendor):
    """When a vendor moves its path, that is a settings change, not a release."""
    base_url, received = vendor
    r = _router({
        "provider": "openai",                 # built in, with its own default URL
        "base_url": base_url,                 # which this overrides
        "model": "gpt-whatever",
        "api_key_env": "TEST_LLM_KEY",
    })
    assert r.complete("sys", "hello").text == "pong"
    assert received[0]["path"] == "/v1/chat/completions"


def test_openai_compatible_without_a_base_url_says_exactly_what_is_missing():
    with pytest.raises(LLMError) as exc:
        _required_base_url({})
    msg = str(exc.value)
    assert "base_url" in msg and "settings.yaml" in msg


@pytest.mark.parametrize("bad", ["ftp://x", "not-a-url", "file:///etc/passwd"])
def test_a_base_url_must_be_http(bad):
    with pytest.raises(LLMError):
        _required_base_url({"base_url": bad})


def test_an_unknown_provider_names_the_way_out(vendor):
    """The error a user hits in 2029 should contain its own fix."""
    base_url, _ = vendor
    r = _router({"provider": "a-vendor-from-the-future", "model": "m",
                 "api_key_env": "TEST_LLM_KEY"})
    with pytest.raises(LLMError) as exc:
        r.complete("sys", "hello")
    msg = str(exc.value)
    assert "openai_compatible" in msg, "the error does not mention the escape hatch"
    assert "openrouter" in msg, "the error does not list what is built in"


# --------------------------------------------------------------------------- #
# the chain
# --------------------------------------------------------------------------- #
def test_a_dead_primary_falls_through_to_a_working_fallback(vendor):
    """A retired model must not take the whole system down."""
    base_url, received = vendor
    r = _router(
        {"provider": "openai_compatible",
         "base_url": "http://127.0.0.1:1/v1",        # nothing listens here
         "model": "retired-in-2028", "api_key_env": "TEST_LLM_KEY"},
        fallbacks=[{"provider": "openai_compatible", "base_url": base_url,
                    "model": "still-alive", "api_key_env": "TEST_LLM_KEY"}],
    )
    assert r.complete("sys", "hello").text == "pong"
    assert received[-1]["body"]["model"] == "still-alive"


def test_a_task_override_changes_the_model_without_touching_the_chain(vendor):
    base_url, received = vendor
    cfg = Config({
        "llm": {
            "primary": {"provider": "openai_compatible", "base_url": base_url,
                        "model": "cheap", "api_key_env": "TEST_LLM_KEY"},
            "fallbacks": [],
            "task_models": {"cover_letter": "expensive"},
            "redact_pii": False,
            "rate_limit": {"requests_per_minute": 6000, "retry_attempts": 1,
                           "retry_backoff_s": [0], "max_concurrent": 4},
        }
    })
    router = Router(cfg, None, None)
    router.complete("sys", "a", task="jd_parse")
    router.complete("sys", "b", task="cover_letter")
    assert [m["body"]["model"] for m in received] == ["cheap", "expensive"]


def test_every_provider_in_the_table_is_callable():
    """A typo in the table is a provider that silently never works."""
    for name, fn in PROVIDERS.items():
        assert callable(fn), f"{name} is not callable"


def test_the_shipped_settings_use_a_provider_that_exists():
    from applier.config import load_settings
    s = load_settings()
    chain = [s.get("llm.primary", {})] + list(s.get("llm.fallbacks", []) or [])
    for spec in chain:
        provider = str(spec.get("provider", ""))
        assert provider in PROVIDERS, (
            f"settings.yaml names provider {provider!r}, which does not exist")


# --------------------------------------------------------------------------- #
# the editor in front of it
# --------------------------------------------------------------------------- #
# _clean_spec is pure validation and writes nothing, so it can be exercised
# directly against the real rules without going near a settings file.
def test_a_valid_spec_survives_validation():
    from applier.web.api import _clean_spec
    out = _clean_spec({"provider": "openai", "model": "gpt-5",
                       "api_key_env": "OPENAI_API_KEY", "temperature": "0.4"},
                      "primary")
    assert out["provider"] == "openai"
    assert out["temperature"] == 0.4, "numeric fields should be cast, not left as text"


@pytest.mark.parametrize("spec,needle", [
    ({"model": "x"},                                   "provider is required"),
    ({"provider": "nope", "model": "x"},               "unknown provider"),
    ({"provider": "openai"},                           "model is required"),
    ({"provider": "openai_compatible", "model": "x"},  "base_url"),
    ({"provider": "openai", "model": "x",
      "base_url": "ftp://y"},                          "http(s)"),
    ({"provider": "openai", "model": "x",
      "api_key": "sk-leaked"},                         "not a model setting"),
])
def test_a_bad_spec_is_refused_with_the_reason(spec, needle):
    from applier.web.api import ApiError, _clean_spec
    with pytest.raises(ApiError) as exc:
        _clean_spec(spec, "primary")
    assert needle in str(exc.value)


def test_an_api_key_cannot_be_written_through_the_model_editor():
    """Keys belong in the keychain, and this endpoint must refuse to take one.

    Accepting `api_key` here would write a secret into settings.local.yaml --
    a plain file, on disk, in a repository directory.
    """
    from applier.web.api import MODEL_FIELDS
    for forbidden in ("api_key", "key", "secret", "password", "token"):
        assert forbidden not in MODEL_FIELDS


def test_the_model_editor_never_returns_a_key():
    from applier.web.api import get_models
    import json
    blob = json.dumps(get_models())
    # api_key_env is a variable NAME and api_key_set is a boolean; neither is
    # a secret. Anything else key-shaped would be.
    blob = blob.replace("api_key_env", "").replace("api_key_set", "")
    assert "api_key" not in blob


# --------------------------------------------------------------------------- #
# a caller that cannot wait forever
# --------------------------------------------------------------------------- #
# The ladder is right for real work: four providers, four attempts each,
# waits up to ninety seconds. It is also twenty minutes in the worst case,
# and preflight was spending it -- 123 seconds of a 132-second run, with
# nothing on screen to say what it was waiting for. `deadline_s` is the
# ceiling a caller can set when somebody is watching.
def _slow_chain(monkeypatch, *, attempts=4, backoff=(30, 90)) -> Router:
    cfg = Config({
        "llm": {
            "primary": {"provider": "always_429", "model": "a",
                        "api_key_env": "KEY_A"},
            "fallbacks": [{"provider": "always_429", "model": "b",
                           "api_key_env": "KEY_B"}],
            "redact_pii": False,
            "rate_limit": {"requests_per_minute": 6000,
                           "retry_attempts": attempts,
                           "retry_backoff_s": list(backoff),
                           "max_concurrent": 4},
        }
    })
    return Router(cfg, None, None)


@pytest.fixture
def always_429(monkeypatch):
    from applier.llm.router import RateLimited

    calls = {"n": 0}

    def provider(spec, system, user, key, want_json):
        calls["n"] += 1
        raise RateLimited("429 slow down")

    monkeypatch.setitem(PROVIDERS, "always_429", provider)
    monkeypatch.setenv("KEY_A", "x")
    monkeypatch.setenv("KEY_B", "y")
    return calls


def test_without_a_deadline_the_ladder_is_unchanged(always_429, monkeypatch):
    """The budget is a ceiling, never a target. Nothing changes without one."""
    slept: list[float] = []
    monkeypatch.setattr("applier.llm.router.time.sleep", slept.append)
    with pytest.raises(LLMError):
        _slow_chain(monkeypatch, attempts=3, backoff=(30, 90)).complete("s", "u")
    assert max(slept, default=0) >= 30, f"expected the full ladder, slept {slept}"


def test_a_deadline_refuses_a_wait_it_cannot_afford(always_429, monkeypatch):
    """Ninety seconds of backoff inside a twenty-five second budget is the
    exact shape of the hang: the wait alone outlives the caller."""
    slept: list[float] = []
    monkeypatch.setattr("applier.llm.router.time.sleep", slept.append)
    with pytest.raises(LLMError) as e:
        _slow_chain(monkeypatch, attempts=3, backoff=(30, 90)).complete(
            "s", "u", deadline_s=5)
    # The inter-request throttle is sub-second and legitimate; a backoff is
    # tens of seconds. Only the second kind may not happen here.
    assert max(slept, default=0) < 1, f"took a backoff-sized wait: {slept}"
    assert "out of time" in str(e.value) or "429" in str(e.value)


def test_a_deadline_caps_each_request_timeout(always_429, monkeypatch):
    """A single request may not outlive the budget that contains it."""
    seen: list[float] = []

    def provider(spec, system, user, key, want_json):
        seen.append(float(spec.get("timeout_s", 90)))
        raise LLMError("nope", retryable=False)

    monkeypatch.setitem(PROVIDERS, "always_429", provider)
    with pytest.raises(LLMError):
        _slow_chain(monkeypatch).complete("s", "u", deadline_s=10)
    assert seen and max(seen) <= 10, f"request timeouts {seen} exceed a 10s budget"


def test_an_exhausted_budget_stops_trying_further_providers(always_429, monkeypatch):
    monkeypatch.setattr("applier.llm.router.time.sleep", lambda s: None)
    with pytest.raises(LLMError) as e:
        _slow_chain(monkeypatch).complete("s", "u", deadline_s=-1)
    assert "out of time" in str(e.value)
    assert always_429["n"] == 0, "nothing should be attempted past the deadline"
