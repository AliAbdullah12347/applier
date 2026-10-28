"""Finding the browser Playwright installed, when Playwright cannot.

Inside a PyInstaller bundle the driver computes the wrong executable path. It
reported

    Executable doesn't exist at
    ...\\ms-playwright\\chromium_headless_shell-1234\\chrome-headless-shell

while the file is actually at

    ...\\chromium_headless_shell-1234\\chrome-headless-shell-win64\\chrome-headless-shell.exe

-- the platform subdirectory and the extension are both missing, which is the
Linux shape of that path. The browser was installed correctly; the frozen
driver just could not say where.

Rather than fight the driver's platform detection, this looks for the file and
hands Playwright an explicit `executable_path`. That is a supported argument,
it costs nothing when the driver is right, and it means a working install is
never reported as a missing one.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Most specific first: the headless shell is what `launch(headless=True)` uses,
# and the full browser is the fallback for a headed run.
_PATTERNS = (
    "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*",
    "chromium-*/chrome-*/chrome*",
)

# The glob above is deliberately loose about the suffix, because the file is
# `chrome` on Linux and `chrome.exe` on Windows. That also makes it match
# `chrome_proxy.exe`, `chrome_pwa_launcher.exe` and `chrome_elf.dll`, which
# sit in the same directory and are not browsers. Handing one of those to
# Playwright as `executable_path` produces a launch failure that looks
# exactly like a missing install.
_EXECUTABLES = {"chrome", "chrome-headless-shell", "headless_shell"}

_BUILD_RE = re.compile(r"(\d+)\s*$")


def browsers_root() -> Path:
    """Where `playwright install` puts things on this machine."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env not in ("0", "1"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "ms-playwright"


def _runnable(path: Path) -> bool:
    if not path.is_file() or path.stem.lower() not in _EXECUTABLES:
        return False
    if sys.platform == "win32":
        return path.suffix.lower() == ".exe"
    return os.access(path, os.X_OK)


def _build(path: Path) -> int:
    """The build number of the directory holding this executable.

    Sorting these as text puts `chromium-999` above `chromium-1234`, so an
    upgrade that left the old build in place would be preferred over the new
    one -- and the driver expects the build matching its own version.
    """
    m = _BUILD_RE.search(path.parent.parent.name)
    return int(m.group(1)) if m else -1


def find_chromium() -> Path | None:
    """The installed Chromium executable, or None if there is not one.

    Returns the newest build when several are present, because Playwright
    upgrades leave the old ones in place and the current driver expects the
    one matching its own version -- which is almost always the newest.
    """
    root = browsers_root()
    if not root.is_dir():
        return None
    for pattern in _PATTERNS:
        found = sorted((p for p in root.glob(pattern) if _runnable(p)),
                       key=_build, reverse=True)
        if found:
            return found[0]
    return None


def launch_kwargs() -> dict:
    """Extra arguments for `chromium.launch`, empty when none are needed.

    Only supplies `executable_path` when a browser is actually found, so a
    machine where the driver resolves correctly is untouched.
    """
    exe = find_chromium()
    return {"executable_path": str(exe)} if exe else {}


def install_hint() -> str:
    """What to tell somebody whose browser is genuinely missing."""
    return (f"No Chromium under {browsers_root()}. Install it once with:  "
            f"playwright install chromium")


def diagnose() -> dict:
    """Everything the browser check knows, so a failure stops being a guess.

    The message this replaces named the path Playwright's driver *computed*
    and nothing about the path this program actually searched. When the two
    disagreed -- which is the whole reason this module exists -- the report
    described a file nobody had looked for, and the only way forward was to
    reason about which of them was wrong.
    """
    root = browsers_root()
    found: list[str] = []
    if root.is_dir():
        for pattern in _PATTERNS:
            found += [str(p) for p in sorted(root.glob(pattern)) if _runnable(p)]
    chosen = find_chromium()
    return {
        "env": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
        "root": str(root),
        "root_exists": root.is_dir(),
        "builds": sorted(p.name for p in root.iterdir()) if root.is_dir() else [],
        "found": found,
        "chosen": str(chosen) if chosen else "",
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def describe_failure(detail: str) -> str:
    """One line a person can act on, built from what was actually searched."""
    d = diagnose()
    if d["chosen"]:
        return (f"a browser was found at {d['chosen']} and still would not "
                f"start. The driver said: {detail[:160]}")
    if not d["root_exists"]:
        return (f"no browser directory at {d['root']}. Nothing has been "
                f"installed there yet.")
    if not d["found"]:
        return (f"{d['root']} exists but holds no runnable Chromium "
                f"(contains: {', '.join(d['builds'][:6]) or 'nothing'}).")
    return (f"found {len(d['found'])} candidate(s) under {d['root']} but none "
            f"could be used: {d['found'][0]}")


def install(timeout: int = 900) -> tuple[bool, str]:
    """Run `playwright install chromium` from inside this program.

    Telling somebody to open a terminal and run a command from a library
    they did not install is a fix that only works for people who did not
    need it. The driver is a node binary shipped with the Python package, so
    this is the same command the CLI would run, run directly -- and it works
    in a frozen build, where there is no `playwright` on PATH at all.
    """
    import subprocess

    try:
        from playwright._impl._driver import (
            compute_driver_executable, get_driver_env,
        )
    except Exception as exc:                            # noqa: BLE001
        return False, f"playwright is not importable here: {exc}"

    try:
        node, cli = compute_driver_executable()
    except Exception as exc:                            # noqa: BLE001
        return False, f"could not locate the playwright driver: {exc}"

    env = dict(get_driver_env())
    # Install where this program looks, not wherever the driver defaults to.
    # Those were allowed to differ, which is one way a browser gets installed
    # and still reported missing.
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_root())
    try:
        proc = subprocess.run([str(node), str(cli), "install", "chromium"],
                              env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"the download did not finish within {timeout}s"
    except OSError as exc:
        return False, f"could not run the playwright driver: {exc}"

    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        return False, out[-600:] or f"exit code {proc.returncode}"
    if find_chromium() is None:
        return False, (f"the install reported success but nothing runnable "
                       f"appeared under {browsers_root()}.\n{out[-300:]}")
    return True, f"chromium is installed at {find_chromium()}"
