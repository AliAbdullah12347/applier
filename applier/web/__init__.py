"""The local GUI: a web app served on loopback, driving the same code as the CLI.

    from applier.web import serve
    serve()

Nothing here reimplements a pipeline stage. Every endpoint calls the function
the corresponding CLI command calls, so the two interfaces cannot drift into
disagreeing about what the system does.
"""

from __future__ import annotations


def serve(host: str = "127.0.0.1", port: int = 8765, *,
          open_browser: bool = True, verbose: bool = False) -> None:
    from .server import GuiServer

    srv = GuiServer(host=host, port=port, verbose=verbose)
    # flush=True matters: when stdout is a pipe rather than a terminal Python
    # buffers it, and the one line the user needs in order to open the app
    # would sit unwritten until the process exited.
    print("\n  applier is running. Open this link:\n", flush=True)
    print(f"      {srv.entry_url}\n", flush=True)
    print("  The part after the # is a session token, minted for this run and", flush=True)
    print("  never written to disk. Keep this window open; Ctrl-C to stop.\n", flush=True)
    srv.serve_forever(open_browser=open_browser)


__all__ = ["serve"]
