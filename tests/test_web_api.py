"""Tests for the GUI's API layer: the autonomy dial and the write allow-lists.

Two things are worth guarding here.

**The autonomy dial** is the control that decides whether this software sends
an application in someone's name without being watched. A level that silently
resolves to "submit" when the user picked "prepare only" is the single worst
bug this project could ship, so the projection onto the underlying settings is
tested directly rather than trusted.

**The write allow-lists** are what stops a crafted request from introducing a
profile key the legal resolver would later read as though a human had vouched
for it. They are also the reason a GUI control that saves nothing is a bug you
find here rather than in the browser: a key missing from the list is refused.
"""

from __future__ import annotations

import pytest

from applier import autonomy
from applier.config import Config
from applier.web import api


# --------------------------------------------------------------------------- #
# the autonomy dial
# --------------------------------------------------------------------------- #
def _settings(**overrides) -> Config:
    base = {"apply": {"mode": "auto"}, "search": {"min_score_to_autoapply": 0.70}}
    cfg = Config(base, "test")
    for k, v in overrides.items():
        cfg.set(k.replace("__", "."), v)
    return cfg


def test_every_level_is_described_for_the_ui():
    described = {d["key"] for d in autonomy.describe()}
    assert described == set(autonomy.ORDER)
    for d in autonomy.describe():
        assert d["label"] and d["blurb"], f"{d['key']} needs a human-readable label"


@pytest.mark.parametrize("level,submits", [
    (autonomy.INVOLVED, False),
    (autonomy.REVIEW, False),
    (autonomy.ASSISTED, True),
    (autonomy.AUTONOMOUS, True),
])
def test_only_the_upper_two_levels_submit(level, submits):
    """The promise made in the UI copy has to be the one the code keeps."""
    assert autonomy.LEVELS[level].submits is submits


def test_non_submitting_levels_never_set_apply_mode_to_auto():
    """`apply.mode == 'auto'` is what actually presses submit downstream."""
    for level in (autonomy.INVOLVED, autonomy.REVIEW):
        cfg = _settings()
        autonomy.apply_level(cfg, level)
        assert cfg.get("apply.mode") != "auto", (
            f"{level!r} claims it never submits but sets apply.mode=auto")


def test_apply_level_projects_all_three_settings():
    cfg = _settings()
    lvl = autonomy.apply_level(cfg, autonomy.ASSISTED)
    assert cfg.get("autonomy.level") == "assisted"
    assert cfg.get("apply.mode") == lvl.apply_mode
    assert cfg.get("search.min_score_to_autoapply") == lvl.min_score


def test_assisted_is_choosier_than_autonomous():
    """Assisted means 'only the confident ones', so its bar must be higher."""
    assert autonomy.LEVELS[autonomy.ASSISTED].min_score > \
           autonomy.LEVELS[autonomy.AUTONOMOUS].min_score


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError):
        autonomy.apply_level(_settings(), "yolo")


def test_round_trip_through_current():
    for level in autonomy.ORDER:
        cfg = _settings()
        autonomy.apply_level(cfg, level)
        assert autonomy.current(cfg) == level


@pytest.mark.parametrize("mode,expected", [
    ("dry_run", autonomy.INVOLVED),
    ("review", autonomy.REVIEW),
])
def test_a_hand_edited_settings_file_is_inferred_not_overwritten(mode, expected):
    """Someone who set apply.mode by hand before this dial existed must not
    have their configuration silently changed underneath them."""
    cfg = Config({"apply": {"mode": mode}}, "test")
    assert autonomy.current(cfg) == expected


@pytest.mark.parametrize("threshold", [0.55, 0.60, 0.70, 0.75, 0.90])
def test_ambiguous_config_never_infers_full_autonomy(threshold):
    """`mode: auto` with no explicit level is ambiguous, and the two readings
    differ on whether this software submits unattended. It must resolve to the
    quieter one — an under-claim costs a click, an over-claim costs an
    application the person never agreed to send."""
    cfg = Config({"apply": {"mode": "auto"},
                  "search": {"min_score_to_autoapply": threshold}}, "test")
    assert autonomy.current(cfg) == autonomy.ASSISTED


def test_full_autonomy_requires_an_explicit_choice():
    cfg = Config({"apply": {"mode": "auto"},
                  "autonomy": {"level": "autonomous"}}, "test")
    assert autonomy.current(cfg) == autonomy.AUTONOMOUS


# --------------------------------------------------------------------------- #
# write allow-lists
# --------------------------------------------------------------------------- #
def test_settings_allow_list_has_a_type_for_every_key():
    for key, caster in api.WRITABLE.items():
        assert caster in (str, int, float, bool), f"{key} has no usable caster"


def test_settings_writes_reject_unknown_keys():
    with pytest.raises(api.ApiError):
        api.put_settings({"updates": {"llm.primary.api_key": "leaked"}})


def test_settings_writes_reject_a_path_outside_the_list():
    for evil in ("apply.artifact_dir", "integrity.log_every_submitted_answer",
                 "__class__", "discovery.sources.simplify.url"):
        with pytest.raises(api.ApiError):
            api.put_settings({"updates": {evil: "x"}})


def test_profile_writes_reject_unknown_keys():
    with pytest.raises(api.ApiError):
        api.put_profile({"updates": {"work_authorization.answers.made_up_field": "Yes"}})


def test_profile_writes_reject_structured_values():
    """A dict or list here would replace a whole subtree, not set a field."""
    with pytest.raises(api.ApiError):
        api.put_profile({"updates": {"identity.full_name": {"$ne": None}}})


def test_redaction_never_masks_a_writable_setting():
    """A masked value that a form can save back is config corruption.

    `api_key_env` holds the NAME of an environment variable, not a key — but
    it contains "api_key", so a substring-matching redactor masked it to
    "GEM***". The settings screen reads that into a field; saving the form
    unchanged would write "GEM***" as the variable to look the key up under,
    and the next run would find no key at all.
    """
    from applier.config import redact
    sample = {
        "llm": {"primary": {
            "api_key_env": "GEMINI_API_KEY",     # a name — must survive
            "api_key": "secret-value-here",      # a value — must be masked
            "model": "gemini-3.5-flash",
        }},
    }
    out = redact(sample)["llm"]["primary"]
    assert out["api_key_env"] == "GEMINI_API_KEY"
    assert out["api_key"].endswith("***")
    assert "secret-value-here" not in str(out)


def test_no_writable_key_is_masked_by_redact():
    """The general form of the above, checked against the real allow-list."""
    from applier.config import redact
    for dotted in api.WRITABLE:
        leaf = dotted.split(".")[-1]
        probe = redact({leaf: "a-real-value-typed-by-a-person"})[leaf]
        assert probe == "a-real-value-typed-by-a-person", (
            f"{dotted} is writable but redact() masks it — the settings form "
            f"would save the mask back over the real value")


def test_a_redacted_placeholder_is_refused_on_write():
    with pytest.raises(api.ApiError, match="redacted"):
        api.put_settings({"updates": {"llm.primary.api_key_env": "GEM***"}})


@pytest.mark.parametrize("key,secret", [
    ("api_key", True), ("password", True), ("token", True),
    ("client_secret", True), ("refresh_token", True), ("github_token", True),
    ("secret_key", True),
    # The near-misses that a substring match gets wrong:
    ("api_key_env", False),        # the NAME of an env var
    ("max_output_tokens", False),  # a size, not a credential
    ("thinking_budget", False),
    ("token_path", False),         # where a token lives, not the token
    ("password_field_name", False),
])
def test_secret_key_detection_matches_on_word_boundaries(key, secret):
    from applier.config import is_secret_key
    assert is_secret_key(key) is secret, f"{key!r} classified wrongly"


def test_no_secret_bearing_key_is_writable_or_readable():
    """Keys are set through the keychain endpoint, never through config."""
    for key in api.WRITABLE:
        assert "api_key" not in key or key.endswith("_env"), (
            f"{key} would put a secret in a config file")


def test_profile_allow_list_covers_the_legal_answers_ali_must_correct():
    """He has to be able to fix these himself; they change with his visa."""
    for k in ("work_authorization.citizenship",
              "work_authorization.answers.authorized_now_us",
              "work_authorization.answers.require_sponsorship"):
        assert k in api.PROFILE_WRITABLE


def test_empty_update_payloads_are_rejected():
    for fn in (api.put_settings, api.put_profile):
        with pytest.raises(api.ApiError):
            fn({"updates": {}})
        with pytest.raises(api.ApiError):
            fn({})


# --------------------------------------------------------------------------- #
# artifact listing
# --------------------------------------------------------------------------- #
def test_artifact_listing_refuses_a_path_outside_the_root(tmp_path):
    """The listing hands the browser relative paths; it must never emit one
    that points outside the artifacts directory."""
    assert api._artifact_listing(str(tmp_path)) == []
    assert api._artifact_listing("../../..") == []
    assert api._artifact_listing(None) == []


def test_only_previewable_suffixes_are_offered():
    dangerous = {".exe", ".dll", ".bat", ".ps1", ".sh", ".yaml", ".yml", ".db", ".env"}
    assert not (api.SAFE_SUFFIXES & dangerous)


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def test_secret_names_must_look_like_env_vars():
    for bad in ("", "lower", "A", "WITH SPACE", "semi;colon", "../../etc"):
        with pytest.raises(api.ApiError):
            api.put_secret({"name": bad, "value": "x"})


def test_empty_secret_value_is_rejected():
    with pytest.raises(api.ApiError):
        api.put_secret({"name": "GEMINI_API_KEY", "value": "   "})
