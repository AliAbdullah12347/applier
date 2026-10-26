"""Nothing may reference a name that is not there.

This is the one static check worth owning. A function body referring to a name
nobody imported parses, imports, and passes every test that does not happen to
call it -- and then raises NameError the first time it runs, which for this
codebase means in the middle of an application.

That is not hypothetical: moving path resolution onto `data_path` left
`applier/apply/browser.py` using it without importing it. Everything passed.
The failure appeared only when the apply pipeline was pointed at a real form,
after the resume had been tailored, in the step that opens the browser.

Deliberately scoped to undefined names. Unused imports and f-strings without
placeholders are style, and a test that fails on style is a test people learn
to skip.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["applier", "scripts", "packaging", "tests"]

# pyflakes messages that mean "this will raise at runtime".
FATAL = ("undefined name", "undefined local", "redefinition of unused",
         "syntax error", "invalid syntax")


def _have_pyflakes() -> bool:
    if shutil.which("pyflakes"):
        return True
    r = subprocess.run([sys.executable, "-m", "pyflakes", "--version"],
                       capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.skipif(not _have_pyflakes(), reason="pyflakes is not installed")
def test_nothing_references_a_name_that_does_not_exist():
    r = subprocess.run([sys.executable, "-m", "pyflakes", *TARGETS],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    output = (r.stdout or "") + (r.stderr or "")
    fatal = [line for line in output.splitlines()
             if any(marker in line.lower() for marker in FATAL)]
    assert not fatal, (
        "name(s) referenced but never defined — these raise NameError the "
        "first time the line runs:\n  " + "\n  ".join(fatal[:12]))


@pytest.mark.skipif(not _have_pyflakes(), reason="pyflakes is not installed")
def test_the_check_still_catches_an_undefined_name(tmp_path):
    """Guard the guard: a silent linter is worse than none."""
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    return nonexistent_name\n", encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pyflakes", str(bad)],
                       capture_output=True, text=True, timeout=120)
    assert "undefined name" in ((r.stdout or "") + (r.stderr or "")).lower()


def test_every_module_imports_cleanly():
    """Catches the subset that breaks at import time, with no dependency."""
    import importlib
    import pkgutil

    import applier

    failed = []
    for mod in pkgutil.walk_packages(applier.__path__, "applier."):
        try:
            importlib.import_module(mod.name)
        except Exception as exc:                        # noqa: BLE001
            failed.append(f"{mod.name}: {type(exc).__name__}: {exc}")
    assert not failed, "modules that will not import:\n  " + "\n  ".join(failed)
