"""The desktop application: a native window around the same local server.

This adds a window. It does not add a third implementation of anything. The
window loads exactly the URL `applier gui` prints, served by the same
`GuiServer`, hitting the same endpoints, which call the same functions the CLI
commands call. A bug fixed in one place is fixed everywhere, which is the only
arrangement that survives three interfaces.

Why a web view rather than a native toolkit: the UI already exists as HTML and
has a tested security boundary around it. A Qt or Tk rewrite would be a second
UI to keep in step with the first, and would still need the server behind it.

The window is a *client* of the server, not a privileged shell. It gets the
same per-launch token as a browser would and cannot reach anything a browser
could not, so the security tests in tests/test_web_security.py still describe
the whole attack surface.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from typing import Any

TITLE = "applier"
MIN_SIZE = (960, 640)
DEFAULT_SIZE = (1340, 900)
READY_TIMEOUT = 20.0


class DesktopError(RuntimeError):
    pass


def _wait_until_serving(host: str, port: int, timeout: float = READY_TIMEOUT) -> None:
    """Block until the server accepts a connection.

    Opening the window before the socket is listening shows a browser error
    page, and the window does not reload on its own -- the first thing the
    user would see is "connection refused" on an application that is in fact
    about to be fine.
    """
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    raise DesktopError(f"the local server did not start within {timeout:g}s ({last})")


def _import_webview() -> Any:
    try:
        import webview                                   # noqa: PLC0415
    except ImportError as exc:                           # pragma: no cover
        raise DesktopError(
            "pywebview is not installed. Install it with:\n"
            "    pip install pywebview\n"
            "or run `applier gui` to use your browser instead."
        ) from exc
    return webview


def run(port: int = 8765, *, verbose: bool = False,
        fallback_to_browser: bool = True) -> int:
    """Open the desktop window. Returns a process exit code.

    The server runs on a daemon thread and the web view owns the main thread,
    because macOS requires the UI to be on thread zero and will abort the
    process otherwise. That ordering is not a preference.
    """
    from .config import ensure_home
    from .web.server import GuiServer

    ensure_home()

    server = GuiServer(port=port, verbose=verbose)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"open_browser": False},
        name="applier-httpd",
        daemon=True,
    )
    thread.start()

    try:
        _wait_until_serving(server.host, server.port)
    except DesktopError as exc:
        print(f"  {exc}", flush=True)
        return 1

    try:
        webview = _import_webview()
    except DesktopError as exc:
        if not fallback_to_browser:
            print(f"  {exc}", flush=True)
            server.shutdown()
            return 1
        # A missing window toolkit is not a reason to be unable to work.
        print(f"  {exc}\n\n  Opening your browser instead:\n", flush=True)
        print(f"      {server.entry_url}\n", flush=True)
        webbrowser.open(server.entry_url)
        try:
            thread.join()
        except KeyboardInterrupt:
            pass
        return 0

    webview.create_window(
        TITLE,
        server.entry_url,
        width=DEFAULT_SIZE[0], height=DEFAULT_SIZE[1],
        min_size=MIN_SIZE,
        confirm_close=False,
        text_select=True,
    )

    code = 0
    try:
        # private_mode=False keeps sessionStorage across a window reopen, which
        # is where the page keeps its token; without it a reload inside the
        # window would land on the pairing screen for no reason the user can see.
        webview.start(private_mode=False)
    except Exception as exc:                             # pragma: no cover
        # The usual cause on Windows is a missing WebView2 runtime. Saying so
        # beats an unhandled traceback, and the browser still works.
        print(f"  Could not open a native window: {exc}", flush=True)
        if fallback_to_browser:
            print(f"\n  Opening your browser instead:\n\n      {server.entry_url}\n",
                  flush=True)
            webbrowser.open(server.entry_url)
            try:
                thread.join()
            except KeyboardInterrupt:
                pass
        else:
            code = 1
    finally:
        server.shutdown()
    return code


def main() -> int:
    """Entry point for the packaged executable.

    A frozen app has no console to print a traceback into, so anything that
    escapes is written next to the user's data where it can be found and sent
    on, rather than vanishing with the process.
    """
    try:
        return run(verbose=bool(os.environ.get("APPLIER_VERBOSE")))
    except Exception:                                    # pragma: no cover
        import traceback
        from .config import ROOT
        try:
            ROOT.mkdir(parents=True, exist_ok=True)
            crash = ROOT / "crash.log"
            with crash.open("a", encoding="utf-8") as fh:
                fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                traceback.print_exc(file=fh)
            print(f"  applier hit an error. Details written to {crash}", flush=True)
        except Exception:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
