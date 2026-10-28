"""Where files live, and the difference between yours and the program's.

Running from a checkout these are the same directory, which is exactly why
the distinction was easy to miss. Inside a packaged application they are not:
PyInstaller unpacks the bundle into a temporary directory and deletes it on
exit. A database, a profile or a content bank resolved relative to the code
would be destroyed every time the window closed -- silently, and only for the
people running the packaged build, which is everyone except the author.

These tests describe the rule that prevents it:

    asset_root()  the program's own files. Read-only. Replaced by an update.
    data_root()   the user's files. Must outlive the program.

and the override that makes both testable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _in_subprocess(code: str, env_extra: dict[str, str] | None = None) -> str:
    """Run a snippet in a fresh interpreter.

    applier.config resolves its roots at import time, so an override has to be
    set before the module is first imported. A subprocess is the honest way to
    test that; reaching in and reassigning the module global would test a
    thing the real program never does.
    """
    env = dict(os.environ)
    env.pop("APPLIER_HOME", None)
    env.update(env_extra or {})
    env["PYTHONUTF8"] = "1"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=str(ROOT), env=env, timeout=120)
    assert r.returncode == 0, f"subprocess failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout.strip()


# --------------------------------------------------------------------------- #
# the override
# --------------------------------------------------------------------------- #
def test_applier_home_moves_the_data_directory(tmp_path):
    out = _in_subprocess(
        "from applier.config import ROOT, CONFIG_DIR, DATA_DIR;"
        "print(ROOT);print(CONFIG_DIR);print(DATA_DIR)",
        {"APPLIER_HOME": str(tmp_path)},
    ).splitlines()
    assert Path(out[0]) == tmp_path.resolve()
    assert Path(out[1]) == tmp_path.resolve() / "config"
    assert Path(out[2]) == tmp_path.resolve() / "data"


def test_the_override_does_not_move_the_programs_own_files(tmp_path):
    """Templates and the web UI ship with the code and must not follow the data."""
    out = _in_subprocess(
        "from applier.config import ASSETS;print(ASSETS)",
        {"APPLIER_HOME": str(tmp_path)},
    )
    assert Path(out) == ROOT
    assert Path(out) != tmp_path.resolve()


def test_without_the_override_a_checkout_behaves_exactly_as_before():
    out = _in_subprocess(
        "from applier.config import ROOT, ASSETS;print(ROOT);print(ASSETS)"
    ).splitlines()
    assert Path(out[0]) == ROOT and Path(out[1]) == ROOT


# --------------------------------------------------------------------------- #
# first run
# --------------------------------------------------------------------------- #
def test_ensure_home_creates_everything_a_first_run_needs(tmp_path):
    out = _in_subprocess(
        "from applier.config import ensure_home, ROOT;"
        "ensure_home();"
        "print('\\n'.join(sorted(str(p.relative_to(ROOT)).replace('\\\\','/')"
        "  for p in ROOT.rglob('*') if p.is_file())))",
        {"APPLIER_HOME": str(tmp_path)},
    )
    files = set(out.splitlines())
    for needed in ("config/settings.yaml", "config/profile.yaml",
                   "config/bank/master_resume.md"):
        assert needed in files, f"a first run did not produce {needed}: {sorted(files)}"
    for d in ("data", "applications", "out"):
        assert (tmp_path / d).is_dir(), f"{d}/ was not created"


def test_ensure_home_never_overwrites_what_is_already_there(tmp_path):
    """The one unforgivable bug in this function: clobbering a real profile."""
    (tmp_path / "config").mkdir(parents=True)
    mine = tmp_path / "config" / "profile.yaml"
    mine.write_text("identity:\n  full_name: Do Not Touch\n", encoding="utf-8")

    for _ in range(2):           # also proves it is idempotent
        _in_subprocess("from applier.config import ensure_home;ensure_home()",
                       {"APPLIER_HOME": str(tmp_path)})

    assert "Do Not Touch" in mine.read_text(encoding="utf-8")


def test_a_fresh_home_does_not_inherit_the_real_profile(tmp_path):
    """A new install must start blank, not with whoever built it."""
    _in_subprocess("from applier.config import ensure_home;ensure_home()",
                   {"APPLIER_HOME": str(tmp_path)})
    seeded = (tmp_path / "config" / "profile.yaml").read_text(encoding="utf-8")
    real = ROOT / "config" / "profile.yaml"
    if real.exists():
        assert seeded != real.read_text(encoding="utf-8"), (
            "a fresh install was seeded with the developer's own profile")


# --------------------------------------------------------------------------- #
# resolution order
# --------------------------------------------------------------------------- #
def test_a_users_own_template_wins_over_the_shipped_one(tmp_path):
    out = _in_subprocess(
        "from pathlib import Path;"
        "from applier.config import resolve_asset, ROOT;"
        "p = ROOT / 'templates' / 'resume' / 'classic.tex.j2';"
        "print(resolve_asset('templates/resume/classic.tex.j2'));"
        "p.parent.mkdir(parents=True, exist_ok=True);"
        "p.write_text('mine', encoding='utf-8');"
        "print(resolve_asset('templates/resume/classic.tex.j2'))",
        {"APPLIER_HOME": str(tmp_path)},
    ).splitlines()
    assert Path(out[0]) == ROOT / "templates" / "resume" / "classic.tex.j2"
    assert Path(out[1]) == tmp_path.resolve() / "templates" / "resume" / "classic.tex.j2"


def test_an_absolute_path_is_returned_unchanged(tmp_path):
    out = _in_subprocess(
        f"from applier.config import resolve_asset;print(resolve_asset({str(tmp_path)!r}))",
        {"APPLIER_HOME": str(tmp_path)},
    )
    assert Path(out) == tmp_path


# --------------------------------------------------------------------------- #
# packaging
# --------------------------------------------------------------------------- #
def test_the_spec_ships_every_directory_the_app_reads_at_runtime():
    spec = (ROOT / "packaging" / "applier.spec").read_text(encoding="utf-8")
    for needed in ("applier/web/static", "templates", "config", "extension"):
        assert needed in spec, f"the bundle would not contain {needed}"


def test_the_spec_keeps_the_lazily_imported_verifier():
    """pdfplumber is imported inside a function, so the bundler cannot see it.

    Without it the frozen app loses PDF verification and says nothing, which
    means resumes ship unchecked from the packaged build only.
    """
    spec = (ROOT / "packaging" / "applier.spec").read_text(encoding="utf-8")
    for mod in ("pdfplumber", "pypdf", "applier.apply.adapters", "webview"):
        assert mod in spec, f"{mod} is not declared as a hidden import"


def test_the_packaged_entry_point_seeds_before_it_dispatches():
    """`applier.exe doctor` on a clean machine reported a missing settings.yaml
    that nothing had been asked to create. The seed has to come first."""
    src = (ROOT / "packaging" / "entry.py").read_text(encoding="utf-8")
    assert "ensure_home" in src
    assert src.index("ensure_home") < src.index("from applier.cli import app")


def test_the_desktop_window_is_a_client_not_a_privileged_shell():
    """It must get the same token a browser gets, and no special route in."""
    src = (ROOT / "applier" / "desktop.py").read_text(encoding="utf-8")
    assert "entry_url" in src, "the window should load the ordinary token URL"
    for backdoor in ("token=", "X-Applier-Token", "SECURITY", "allow_origin"):
        assert backdoor not in src, f"desktop.py appears to bypass auth: {backdoor}"


@pytest.mark.parametrize("name", ["build.py", "entry.py", "applier.spec"])
def test_packaging_files_exist(name):
    assert (ROOT / "packaging" / name).is_file()


# --------------------------------------------------------------------------- #
# relative paths in settings
# --------------------------------------------------------------------------- #
def test_a_relative_settings_path_means_the_data_directory(tmp_path):
    """Not "whatever directory this was launched from".

    Those were the same thing until there was a packaged build. Double-clicking
    an executable sets the working directory to its own folder, so `config/bank`
    pointed at nothing -- and running the packaged app from a checkout made it
    silently read that checkout's real bank instead of the one it had seeded.
    """
    out = _in_subprocess(
        "from applier.config import data_path;"
        "print(data_path('config/bank'));"
        "print(data_path('applications'))",
        {"APPLIER_HOME": str(tmp_path)},
    ).splitlines()
    assert Path(out[0]) == tmp_path.resolve() / "config" / "bank"
    assert Path(out[1]) == tmp_path.resolve() / "applications"


def test_an_absolute_settings_path_is_left_alone(tmp_path):
    out = _in_subprocess(
        f"from applier.config import data_path;print(data_path({str(tmp_path)!r}))",
        {"APPLIER_HOME": str(tmp_path / 'elsewhere')},
    )
    assert Path(out) == tmp_path


def test_nothing_resolves_a_settings_path_against_the_working_directory():
    """One escaped site is one install reading the wrong bank."""
    import re as _re
    for rel in ("applier/cli.py", "applier/pipeline.py", "applier/preflight.py",
                "applier/tailor/pipeline.py", "applier/outreach/draft.py",
                "applier/apply/browser.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for m in _re.finditer(r'Path\((?:settings|self\.s)\.get\("([\w.]+)"', src):
            raise AssertionError(
                f"{rel} resolves {m.group(1)} against the working directory; "
                f"use data_path()")


# --------------------------------------------------------------------------- #
# what a brand-new install does
# --------------------------------------------------------------------------- #
def test_a_fresh_install_does_not_submit_applications(tmp_path):
    """The default somebody gets before they have configured anything.

    With no autonomy.level the inference reads `apply.mode: auto` as
    "assisted", which submits by itself -- on an install whose profile is
    still full of placeholders. The shipped settings now say so explicitly.
    """
    out = _in_subprocess(
        "from applier.config import ensure_home, load_settings;"
        "ensure_home();"
        "from applier import autonomy;"
        "s = load_settings(reload=True);"
        "lvl = autonomy.current(s);"
        "print(lvl);print(autonomy.LEVELS[lvl].submits)",
        {"APPLIER_HOME": str(tmp_path)},
    ).splitlines()
    assert out[0] == "involved", f"a fresh install defaults to {out[0]!r}"
    assert out[1] == "False", "a fresh install would submit applications by itself"


def test_the_shipped_settings_name_the_level_explicitly():
    """Relying on inference is how the dangerous default got there."""
    import yaml
    data = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    assert (data.get("autonomy") or {}).get("level") == "involved"


# --------------------------------------------------------------------------- #
# the browser the bundle cannot find
# --------------------------------------------------------------------------- #
def test_the_browser_is_located_by_us_not_by_the_frozen_driver():
    """Inside a bundle the driver computes a non-Windows path.

    It reported the executable missing at
    `chromium_headless_shell-1234/chrome-headless-shell` while the file was at
    `chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe`
    -- no platform subdirectory and no extension. The browser was installed
    correctly; the driver just could not say where.
    """
    from applier.browsers import find_chromium, launch_kwargs

    found = find_chromium()
    if found is None:
        import pytest
        pytest.skip("no browsers installed on this machine")
    assert found.is_file()
    assert "executable_path" in launch_kwargs()


def test_launch_kwargs_are_empty_when_there_is_no_browser(monkeypatch, tmp_path):
    """A machine where the driver resolves correctly must be untouched."""
    from applier import browsers

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nothing"))
    assert browsers.launch_kwargs() == {}


def test_every_launch_site_names_the_executable():
    """One escaped site is one install reporting a working browser as missing."""
    for rel, needle in (("applier/preflight.py", "launch_kwargs"),
                        ("applier/outreach/people.py", "launch_kwargs"),
                        ("applier/apply/browser.py", "launch_kwargs")):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert needle in src, f"{rel} launches a browser without locating it"


# --------------------------------------------------------------------------- #
# the audit cannot shell out to itself
# --------------------------------------------------------------------------- #
def test_the_privacy_audit_does_not_run_the_app_as_an_interpreter():
    """In a packaged build sys.executable is applier.exe, not a Python.

    `[sys.executable, script]` therefore ran `applier.exe <path>`, and the CLI
    answered "No such command" -- the audit that gates a push could not run at
    all from the app.
    """
    import inspect

    from applier.web import api

    src = inspect.getsource(api.run_audit) if hasattr(api, "run_audit") else ""
    if not src:
        for name in dir(api):
            fn = getattr(api, name)
            if callable(fn) and "audit" in name.lower():
                try:
                    src = inspect.getsource(fn)
                except (OSError, TypeError):
                    continue
                if "audit_privacy" in src:
                    break
    assert "audit_privacy" in src, "could not find the audit handler"
    assert 'getattr(sys, "frozen"' in src, (
        "the audit still shells out to sys.executable in a frozen build")
    assert "runpy" in src
