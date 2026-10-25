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
