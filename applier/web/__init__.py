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
    print(f"\n  applier is running at  {srv.url}")
    print(f"  Keep this window open. Ctrl-C to stop.\n")
    srv.serve_forever(open_browser=open_browser)


__all__ = ["serve"]
