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
import sys
from pathlib import Path

# Most specific first: the headless shell is what `launch(headless=True)` uses,
# and the full browser is the fallback for a headed run.
_PATTERNS = (
    "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell*",
    "chromium-*/chrome-*/chrome*",
)


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
    if not path.is_file():
        return False
    if sys.platform == "win32":
        return path.suffix.lower() == ".exe"
    return os.access(path, os.X_OK)


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
                       key=lambda p: p.parent.parent.name, reverse=True)
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
