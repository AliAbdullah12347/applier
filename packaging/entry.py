"""Entry point for the packaged desktop application.

Kept separate from `applier/__main__.py` because that one is a CLI and this one
is an app: a double-clicked executable has no terminal, so there is nowhere for
an argument-parsing error or a traceback to go. This starts the window and
nothing else.

A packaged build still carries the full CLI -- pass arguments and you get it:

    applier.exe doctor
    applier.exe apply <url> --review

With no arguments, it opens the window.
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Required before anything else on a frozen Windows build: without it, any
    # library that spawns a process re-launches the whole executable instead,
    # and the app opens itself in an endless loop of windows.
    multiprocessing.freeze_support()

    # Seed the per-user data directory before anything tries to read it. The
    # window did this itself; the CLI did not, so `applier.exe doctor` on a
    # fresh install reported a missing settings.yaml for a file nothing had
    # yet been asked to create. Idempotent, and it never overwrites.
    try:
        from applier.config import ensure_home
        ensure_home()
    except Exception as exc:                             # pragma: no cover
        print(f"  could not prepare the data directory: {exc}", flush=True)
        return 1

    if len(sys.argv) > 1:
        from applier.cli import app
        app()
        return 0

    from applier.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    sys.exit(main())
