"""The browser is the only thing that parses the UI, and it does so at runtime.

A syntax error in a panel is invisible to every other check in this suite. The
module fails to import, the panel renders as an empty box, and the app looks
merely plain rather than broken -- which is how one shipped: a single missing
closing parenthesis in a card that had just been written, found by opening the
page in a real browser rather than by reading the diff.

Python cannot check this and neither can a regex: the file contains regex
literals, whose slashes and parentheses defeat any counter naive enough to be
worth writing. So the check shells out to a real JavaScript parser.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "applier" / "web" / "static"
NODE = shutil.which("node")

FILES = sorted([STATIC / "app.js", *(STATIC / "panels").glob("*.js")])


def _parse(path: Path, tmp: Path) -> tuple[int, str]:
    """Parse as an ES module. The extension is what selects module semantics."""
    copy = tmp / (path.stem + ".mjs")
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(copy)], capture_output=True,
                       text=True, timeout=60)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_every_panel_parses(path, tmp_path):
    code, out = _parse(path, tmp_path)
    assert code == 0, f"{path.name} does not parse:\n{out.strip()[:600]}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_parser_actually_rejects_bad_javascript(tmp_path):
    """Guard the guard.

    A check that silently stops checking is worse than no check, and this one
    runs an external binary whose behaviour is not ours to assume.
    """
    bad = tmp_path / "bad.mjs"
    bad.write_text("export const x = foo(bar(1);\n", encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(bad)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0, "node accepted an unbalanced call — the guard is dead"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_missing_closing_paren_in_a_real_panel_is_caught(tmp_path):
    """The exact bug that shipped, reproduced against the real file."""
    src = (STATIC / "panels" / "settings.js").read_text(encoding="utf-8")
    marker = "')));"
    assert marker in src, "the shape of the bug no longer exists to test against"
    broken = tmp_path / "broken.mjs"
    broken.write_text(src.replace(marker, "'));", 1), encoding="utf-8")
    r = subprocess.run([NODE, "--check", str(broken)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode != 0, (
        "removing a closing parenthesis from settings.js still parsed; "
        "this test is no longer testing anything")
