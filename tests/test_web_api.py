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
