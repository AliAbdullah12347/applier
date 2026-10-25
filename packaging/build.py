"""Build the desktop application.

    python packaging/build.py

Produces `dist/applier.exe` on Windows and `dist/applier.app` on macOS. There
is no cross-compilation: a macOS build must run on a Mac, because the bundle
embeds a platform Python and a platform web-view backend. That is a property
of the format, not a shortcut taken here.

The build is checked after it is produced. An executable that builds and then
cannot start is the normal failure mode for a frozen Python app -- a module
reached by name rather than by import gets left out, and nothing says so until
someone double-clicks it.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "applier.spec"
DIST = ROOT / "dist"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"  $ {' '.join(cmd[:4])}{' …' if len(cmd) > 4 else ''}", flush=True)
    return subprocess.run(cmd, cwd=str(ROOT), **kw)


def preflight() -> list[str]:
    """Things that must be true before building. Returns a list of problems."""
    problems: list[str] = []
    try:
        import PyInstaller                                     # noqa: F401,PLC0415
    except ImportError:
        problems.append("PyInstaller is not installed:  pip install pyinstaller")
    try:
        import webview                                         # noqa: F401,PLC0415
    except ImportError:
        problems.append("pywebview is not installed:  pip install pywebview")
    if not SPEC.exists():
        problems.append(f"missing spec file: {SPEC}")
    if sys.version_info < (3, 11):
        problems.append(f"Python 3.11+ required, found {platform.python_version()}")
    return problems


def smoke_test(exe: Path) -> list[str]:
    """Start the built binary and prove the parts that usually fall out.

    Not a substitute for opening the window -- it checks the things a missing
    hidden import breaks, which is everything that is reached by name.
    """
    failures: list[str] = []

    checks = [
        (["--help"], "applier", "the CLI did not start"),
        (["doctor"], "pdflatex", "doctor did not run"),
    ]
    for args, expect, why in checks:
        try:
            r = subprocess.run([str(exe), *args], capture_output=True, text=True,
                               timeout=180)
        except subprocess.TimeoutExpired:
            failures.append(f"{' '.join(args)}: timed out — {why}")
            continue
        out = (r.stdout or "") + (r.stderr or "")
        if expect.lower() not in out.lower():
            failures.append(f"{' '.join(args)}: {why}\n      {out.strip()[:300]}")

    # The verifier is optional-import-shaped and is the one thing that must not
    # disappear silently: without it, resumes ship unchecked.
    probe = (
        "import sys;"
        "import pdfplumber, pypdf;"
        "from applier.tailor.render import verify_pdf, squeezed_words;"
        "from applier.web.server import GuiServer;"
        "from applier.llm.router import Router;"
        "print('IMPORTS OK')"
    )
    try:
        r = subprocess.run([str(exe), "selftest", "--code", probe],
                           capture_output=True, text=True, timeout=120)
        if "IMPORTS OK" not in (r.stdout or ""):
            failures.append("selftest: bundled imports incomplete\n      "
                            + ((r.stdout or "") + (r.stderr or "")).strip()[:300])
    except subprocess.TimeoutExpired:
        failures.append("selftest: timed out")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the applier desktop app.")
    ap.add_argument("--clean", action="store_true", help="Remove build/ and dist/ first.")
    ap.add_argument("--skip-tests", action="store_true",
                    help="Do not smoke-test the built binary.")
    args = ap.parse_args()

    print(f"\napplier build — {platform.system()} {platform.machine()}, "
          f"Python {platform.python_version()}\n")

    problems = preflight()
    if problems:
        print("Cannot build:")
        for p in problems:
            print(f"  ! {p}")
        return 1

    if args.clean:
        for d in (ROOT / "build", DIST):
            if d.exists():
                shutil.rmtree(d)
                print(f"  removed {d.name}/")

    started = time.time()
    env = dict(os.environ, PYTHONUTF8="1")
    r = _run([sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm",
              "--log-level", "WARN"], env=env)
    if r.returncode != 0:
        print(f"\nPyInstaller failed with exit code {r.returncode}.")
        return r.returncode

    exe = DIST / ("applier.exe" if sys.platform == "win32" else "applier")
    bundle = DIST / "applier.app"
    target = bundle if bundle.exists() else exe
    if not target.exists():
        print(f"\nBuild reported success but produced nothing at {target}.")
        return 1

    size = (sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            if target.is_dir() else target.stat().st_size)
    print(f"\n  built {target.name} — {size / 1_048_576:.1f} MB "
          f"in {time.time() - started:.0f}s")

    if args.skip_tests:
        print("  (smoke test skipped)")
        return 0

    print("\n  checking the build…")
    failures = smoke_test(exe if exe.exists() else target)
    if failures:
        print("\n  the binary built but does not work:")
        for f in failures:
            print(f"    ! {f}")
        return 1

    print("  all checks passed.\n")
    print(f"  Double-click:  {target}")
    print("  Data lives in the per-user directory, not beside the executable;")
    print("  run `applier doctor` to see exactly where.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
