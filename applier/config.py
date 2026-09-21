"""Configuration loading.

Three layers, later ones winning:

    1. config/settings.yaml + config/profile.yaml   (the files you edit)
    2. environment variables                        (APPLIER__LLM__PRIMARY__MODEL=...)
    3. explicit CLI flags                           (handled in cli.py)

Secrets are never read from the YAML. An LLM key is looked up by the
`api_key_env` name in settings, first in the environment, then in the OS
keychain. That way `settings.yaml` stays safe to commit.
"""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
ENV_PREFIX = "APPLIER__"
ENV_SEP = "__"

# Keys whose values must never be logged, echoed, or sent to a model.
SECRET_KEYS = {"api_key", "password", "token", "secret", "client_secret", "refresh_token"}


class ConfigError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Missing config file: {path}\n"
            f"If this is profile.yaml, copy config/profile.example.yaml to "
            f"config/profile.yaml and run `applier setup`."
        )
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level.")
    return data


def _coerce(raw: str) -> Any:
    """Turn an env-var string into the obvious Python type."""
    low = raw.strip().lower()
    if low in {"true", "yes", "on"}:
        return True
    if low in {"false", "no", "off"}:
        return False
    if low in {"null", "none", ""}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    # JSON-ish list: APPLIER__SEARCH__SEASONS=[a,b]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [p.strip() for p in inner.split(",")] if inner else []
    return raw


def _apply_env_overrides(cfg: dict[str, Any]) -> list[str]:
    """APPLIER__A__B__C=value  ->  cfg['a']['b']['c'] = value. Returns applied paths."""
    applied: list[str] = []
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX):].split(ENV_SEP) if p]
        if not path:
            continue
        node = cfg
        for part in path[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[path[-1]] = _coerce(raw)
        applied.append(".".join(path))
    return applied


def _deep_merge(base: dict, override: dict) -> dict:
    out = deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# dotted access
# --------------------------------------------------------------------------- #
_MISSING = object()


class Config:
    """Dict wrapper with dotted lookup: cfg.get('llm.primary.model')."""

    def __init__(self, data: dict[str, Any], source: str = "") -> None:
        self._data = data
        self.source = source

    def get(self, dotted: str, default: Any = _MISSING) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                if default is _MISSING:
                    raise ConfigError(f"Missing config key: {dotted}  (in {self.source})")
                return default
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def require(self, dotted: str) -> Any:
        return self.get(dotted)

    @property
    def raw(self) -> dict[str, Any]:
        return self._data

    def __contains__(self, dotted: str) -> bool:
        return self.get(dotted, None) is not None

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Config {self.source} keys={list(self._data)}>"


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def get_secret(env_name: str, *, service: str = "applier", required: bool = False) -> str | None:
    """Env var first, then the OS keychain. Never the YAML files."""
    val = os.environ.get(env_name)
    if val:
        return val.strip()
    try:
        import keyring  # imported lazily: it pulls in OS bindings

        val = keyring.get_password(service, env_name)
        if val:
            return val.strip()
    except Exception:  # keyring unavailable or locked -- not fatal
        pass
    if required:
        raise ConfigError(
            f"No value for {env_name}.\n"
            f"Set it with:  applier config set-key {env_name}\n"
            f"or export it: set {env_name}=...    (Windows)   /   export {env_name}=...  (POSIX)"
        )
    return None


def set_secret(env_name: str, value: str, *, service: str = "applier") -> None:
    import keyring

    keyring.set_password(service, env_name, value)


def redact(obj: Any) -> Any:
    """Deep-copy with secret-looking values masked. Use before logging anything."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in SECRET_KEYS) and isinstance(v, str) and v:
                out[k] = v[:3] + "***" if len(v) > 3 else "***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- #
# public entry points
# --------------------------------------------------------------------------- #
_settings_cache: Config | None = None
_profile_cache: Config | None = None


def load_settings(path: Path | None = None, *, reload: bool = False) -> Config:
    global _settings_cache
    if _settings_cache is not None and not reload:
        return _settings_cache
    p = path or CONFIG_DIR / "settings.yaml"
    data = _read_yaml(p)

    # A machine-written overlay sits on top of the hand-written base, so that
    # saving a toggle in the GUI cannot strip the comments that explain every
    # other knob in settings.yaml. Precedence, lowest to highest:
    #   settings.yaml  ->  settings.local.yaml  ->  APPLIER__* environment
    # The overlay is gitignored: it is where a published checkout accumulates
    # one particular person's choices.
    if path is None:
        local = CONFIG_DIR / "settings.local.yaml"
        if local.exists():
            data = _deep_merge(data, _read_yaml(local))

    _apply_env_overrides(data)
    _settings_cache = Config(data, source=str(p))
    return _settings_cache


def load_profile(path: Path | None = None, *, reload: bool = False) -> Config:
    global _profile_cache
    if _profile_cache is not None and not reload:
        return _profile_cache
    p = path or CONFIG_DIR / "profile.yaml"
    if not p.exists():
        example = CONFIG_DIR / "profile.example.yaml"
        raise ConfigError(
            f"No profile at {p}.\n"
            f"Run:  copy \"{example}\" \"{p}\"   then:  applier setup"
        )
    _profile_cache = Config(_read_yaml(p), source=str(p))
    return _profile_cache


def save_profile(cfg: Config, path: Path | None = None) -> None:
    """Persist the profile. This is how 'ask once, remember forever' works."""
    p = path or CONFIG_DIR / "profile.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        yaml.safe_dump(cfg.raw, fh, sort_keys=False, allow_unicode=True, width=100)
    tmp.replace(p)


def save_settings(cfg: Config, path: Path | None = None) -> None:
    """Persist settings.yaml.

    Written atomically via a temp file and a rename, like the profile. The GUI
    saves on every toggle, so a crash or a power cut lands mid-write far more
    often than it would with hand-editing; a half-written settings.yaml would
    take the whole system down on next start.

    Comments in the file are lost on save — PyYAML round-trips values, not
    trivia. settings.yaml is heavily commented on purpose, so the GUI writes
    only the keys a user actually changed, through `patch_settings`, and leaves
    the file alone otherwise.
    """
    p = path or CONFIG_DIR / "settings.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        yaml.safe_dump(cfg.raw, fh, sort_keys=False, allow_unicode=True, width=100)
    tmp.replace(p)
    global _settings_cache
    _settings_cache = cfg


def settings_overlay_path() -> Path:
    """Where GUI-changed settings live.

    Keeping them in a separate overlay preserves the comments in settings.yaml,
    which are the only documentation for half of these knobs. The overlay is
    small, machine-written, and deep-merged over the base at load time.
    """
    return CONFIG_DIR / "settings.local.yaml"


def patch_settings(updates: dict[str, Any]) -> Config:
    """Apply dotted-key updates to the overlay and return the merged settings."""
    p = settings_overlay_path()
    overlay = _read_yaml(p) if p.exists() else {}
    scratch = Config(overlay, source=str(p))
    for dotted, value in updates.items():
        scratch.set(dotted, value)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        yaml.safe_dump(scratch.raw, fh, sort_keys=False, allow_unicode=True, width=100)
    tmp.replace(p)
    return load_settings(reload=True)


def unanswered(cfg: Config) -> list[str]:
    """Every dotted path still set to the ASK sentinel."""
    out: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")
        elif isinstance(node, str) and node.strip().upper() == "ASK":
            out.append(prefix)

    walk(cfg.raw, "")
    return out


def project_paths() -> dict[str, Path]:
    return {
        "root": ROOT,
        "config": CONFIG_DIR,
        "data": DATA_DIR,
        "db": DATA_DIR / "applier.db",
        "artifacts": ROOT / "applications",
        "templates": ROOT / "templates",
        "out": ROOT / "out",
    }
