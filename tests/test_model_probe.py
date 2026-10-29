"""Measuring the chain, because free-tier quotas move.

A fallback chain is only worth its order, and Ali's ran exactly backwards:

    gemini-3.5-flash  ->  gemini-3.8-flash  ->  flash-latest  ->  flash-lite
    429 immediately       75s timeout          503              1.7s, correct

Every call paid the first three before reaching the one that worked, which
is where preflight's 123 seconds came from. None of it was visible: the
chain reports whichever model eventually answered, never the ones that
burned a minute first.
"""

from __future__ import annotations

import pytest

from applier.config import Config
from applier.llm.probe import (
    Probe, as_chain, probe_chain, rank, suggest_order, would_change,
)
from applier.llm.router import PROVIDERS, LLMError, LLMResponse, RateLimited


def _settings(models: list[str]) -> Config:
    spec = lambda m: {"provider": "probe_fake", "model": m,
                      "api_key_env": "PROBE_KEY", "timeout_s": 90}
    return Config({"llm": {"primary": spec(models[0]),
                           "fallbacks": [spec(m) for m in models[1:]]}})


@pytest.fixture
def fake_provider(monkeypatch):
    """Each model behaves however the test says, and records its timeout."""
    behaviour: dict = {}
    seen: dict = {}

    def provider(spec, system, user, key, want_json):
        model = spec["model"]
        seen[model] = spec.get("timeout_s")
        what = behaviour.get(model, "ok")
        if what == "ok":
            return LLMResponse(text="ready.", provider="probe_fake",
                               model=model, output_tokens=2)
        if what == "429":
            raise RateLimited("rate limited: {\n  \"error\": {\n  \"code\": 429")
        if what == "timeout":
            raise LLMError("timeout: The read operation timed out", retryable=True)
        if what == "503":
            raise LLMError("server 503: overloaded", retryable=True)
        if what == "empty":
            return LLMResponse(text="", provider="probe_fake", model=model,
                               output_tokens=0)
        raise LLMError(what)

    monkeypatch.setitem(PROVIDERS, "probe_fake", provider)
    monkeypatch.setenv("PROBE_KEY", "x")
    return behaviour, seen


# --------------------------------------------------------------------------- #
# what it finds
# --------------------------------------------------------------------------- #
def test_each_link_is_classified(fake_provider):
    behaviour, _ = fake_provider
    behaviour.update({"a": "429", "b": "timeout", "c": "503", "d": "ok"})
    states = {p.model: p.state for p in probe_chain(_settings(list("abcd")))}
    assert states == {"a": "rate_limited", "b": "timeout",
                      "c": "unavailable", "d": "ok"}


def test_a_missing_key_is_not_a_broken_model(fake_provider, monkeypatch):
    monkeypatch.delenv("PROBE_KEY", raising=False)
    assert probe_chain(_settings(["a"]))[0].state == "no_key"


def test_an_unknown_provider_is_reported_not_raised():
    cfg = Config({"llm": {"primary": {"provider": "nope", "model": "m"},
                          "fallbacks": []}})
    assert probe_chain(cfg)[0].state == "error"


def test_an_answer_that_is_not_an_answer_counts_as_broken(fake_provider):
    behaviour, _ = fake_provider
    behaviour["a"] = "empty"
    assert probe_chain(_settings(["a"]))[0].state == "error"


def test_the_detail_is_one_line(fake_provider):
    """Providers answer errors with pretty-printed JSON; a table needs a line."""
    behaviour, _ = fake_provider
    behaviour["a"] = "429"
    detail = probe_chain(_settings(["a"]))[0].detail
    assert "\n" not in detail and len(detail) < 80, repr(detail)


def test_probing_uses_the_real_spec_but_a_bounded_timeout(fake_provider):
    """A thinking model reads as broken if probed with a smaller token budget:
    the reasoning eats the allowance and the reply comes back empty."""
    behaviour, seen = fake_provider
    probe_chain(_settings(["a"]), timeout_s=7)
    assert seen["a"] == 7, "the 90s production timeout must not be used here"


# --------------------------------------------------------------------------- #
# the order it suggests
# --------------------------------------------------------------------------- #
def _p(model, state, seconds):
    return Probe(model, "x", "fallback", state, seconds)


def test_a_working_model_goes_first():
    order = suggest_order([_p("slow429", "rate_limited", 0.4),
                           _p("works", "ok", 9.0)])
    assert order[0].model == "works"


def test_the_fastest_working_model_goes_first():
    order = suggest_order([_p("slow", "ok", 9.0), _p("quick", "ok", 1.2)])
    assert [p.model for p in order] == ["quick", "slow"]


def test_among_failures_the_cheapest_to_try_goes_first():
    """Every link pays its own timeout before the next one starts, so a model
    that 429s in under a second belongs ahead of one that burns seventy-five."""
    order = suggest_order([_p("expensive", "timeout", 75.0),
                           _p("cheap", "rate_limited", 0.4)])
    assert [p.model for p in order] == ["cheap", "expensive"]


def test_the_real_chain_that_caused_this():
    probes = [_p("gemini-3.5-flash", "rate_limited", 0.8),
              _p("gemini-3.8-flash", "timeout", 75.0),
              _p("gemini-flash-latest", "unavailable", 6.2),
              _p("gemini-3.1-flash-lite", "ok", 1.7)]
    assert [p.model for p in suggest_order(probes)] == [
        "gemini-3.1-flash-lite", "gemini-3.5-flash",
        "gemini-flash-latest", "gemini-3.8-flash"]


def test_an_order_already_right_is_left_alone():
    assert not would_change([_p("works", "ok", 1.0), _p("bad", "timeout", 20.0)])
    assert would_change([_p("bad", "timeout", 20.0), _p("works", "ok", 1.0)])


def test_the_suggestion_is_shaped_for_the_existing_route(fake_provider):
    behaviour, _ = fake_provider
    behaviour.update({"a": "timeout", "b": "ok"})
    chain = as_chain(probe_chain(_settings(["a", "b"])))
    assert chain["primary"]["model"] == "b"
    assert [f["model"] for f in chain["fallbacks"]] == ["a"]
    assert "api_key_env" in chain["primary"], "the spec must survive intact"


def test_probing_writes_nothing(fake_provider, tmp_path, monkeypatch):
    behaviour, _ = fake_provider
    behaviour.update({"a": "ok", "b": "timeout"})
    cfg = _settings(["a", "b"])
    before = repr(cfg.get("llm.primary"))
    probe_chain(cfg)
    assert repr(cfg.get("llm.primary")) == before
