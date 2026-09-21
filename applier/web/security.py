"""Security policy for the local GUI server.

A server on 127.0.0.1 feels private and is not. Three distinct things can reach
it, and each needs a different defence:

1. **Any website you have open.** A page on evil.example can issue
   `fetch('http://127.0.0.1:8765/api/...')`. The browser sends it. Same-origin
   policy stops the attacker *reading* the reply, but a plain POST still lands —
   that is CSRF, and here it would mean a stranger's page firing off job
   applications under Ali's name.

2. **DNS rebinding.** The attacker points `attacker.com` at 127.0.0.1 after the
   page loads. Now the request is same-origin as far as the browser is
   concerned, so they *can* read the reply — including the profile, which holds
   a date of birth, a home address and an immigration status. Origin checks do
   not help here; the Host header does, because the browser sends the name it
   dialled, and it is not `127.0.0.1`.

3. **Other processes on the machine.** Any local program can connect. Nothing
   in HTTP distinguishes them, so the only real control is a secret.

The defences, therefore:

* **Bind loopback only.** Refuse to start otherwise. Not a flag anyone can flip
  by accident — an externally bound instance would expose the whole profile to
  the local network.
* **A per-launch bearer token** in a custom header. Custom headers cannot be
  set cross-origin without a preflight, and the preflight fails (below). Covers
  (1) and (3).
* **Host allow-list.** Only `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`.
  Covers (2).
* **Origin allow-list.** Present-and-foreign is rejected outright. Absent is
  allowed, because same-origin GETs and non-browser clients legitimately omit it.
* **No CORS headers, ever.** There is no `Access-Control-Allow-Origin` in any
  response, so every cross-origin preflight fails closed.
* **A restrictive CSP** with no remote origins, so an injected string cannot
  phone home even if it does execute.

The token lives in memory for the life of the process. It is never written to
disk, never logged, and never placed in a query string — query strings reach
the server, land in logs, and stick around in browser history.

**How the browser gets it.** In the URL *fragment*: `http://127.0.0.1:8765/#t=…`.
A fragment is never transmitted — the browser strips it before sending the
request — so the token cannot appear in a server log or a referrer. The page
script reads `location.hash`, moves it into `sessionStorage`, and clears the
address bar.

The rejected alternative was embedding the token in `index.html`. That fails
the moment you notice `index.html` must be fetchable without a token (a
`<link>` or `<script src>` tag cannot set a custom header), which would let
any local process simply `curl` the page and read the token out of it.

So the static files — the same HTML, CSS and JS that are published in the
public repository — are served unauthenticated, and carry no data. Everything
under `/api/` requires the token.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets

TOKEN_HEADER = "X-Applier-Token"
MAX_BODY_BYTES = 4 * 1024 * 1024        # generous for a pasted JD, far below a DoS

# No remote origins of any kind. Everything the page needs is served from here,
# which is also why the UI ships no CDN fonts or script tags.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-src 'self' blob:; "          # the embedded PDF preview
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
}

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def is_loopback(host: str) -> bool:
    """True only for an address that cannot be reached from another machine."""
    h = (host or "").strip().strip("[]")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _split_hostport(value: str) -> tuple[str, str | None]:
    """Split `host:port`, tolerating the bracketed IPv6 form."""
    v = (value or "").strip()
    if v.startswith("["):
        host, _, rest = v.partition("]")
        return host.lstrip("["), rest.lstrip(":") or None
    if v.count(":") == 1:
        host, _, port = v.partition(":")
        return host, port or None
    return v, None


def check_host(header: str | None, port: int) -> str | None:
    """Reject anything but a loopback name on our own port (DNS rebinding).

    Returns an error string, or None when the request is acceptable.
    """
    if not header:
        # HTTP/1.1 requires Host. Its absence means a hand-rolled client, and
        # we have no way to tell which name it dialled.
        return "missing Host header"
    host, hport = _split_hostport(header)
    if host.lower() not in LOOPBACK_NAMES and not is_loopback(host):
        return f"host {header!r} is not loopback (possible DNS rebinding)"
    if hport is not None and hport != str(port):
        return f"host port {hport} does not match server port {port}"
    return None


def check_origin(origin: str | None, port: int) -> str | None:
    """Reject a cross-origin caller. Absent Origin is allowed.

    Browsers omit Origin on same-origin GETs and on top-level navigation, and
    non-browser clients omit it entirely; treating absence as hostile would
    break both without buying anything, since the token is still required.
    """
    if not origin or origin == "null":
        return None
    scheme, _, rest = origin.partition("://")
    if scheme not in ("http", "https"):
        return f"origin scheme {scheme!r} not allowed"
    host, oport = _split_hostport(rest)
    if host.lower() not in LOOPBACK_NAMES and not is_loopback(host):
        return f"cross-origin request from {origin!r} refused"
    if oport is not None and oport != str(port):
        return f"origin port {oport} does not match server port {port}"
    return None


def check_token(supplied: str | None, expected: str) -> str | None:
    """Constant-time comparison, so a wrong guess leaks no timing signal."""
    if not supplied:
        return "missing session token"
    if not hmac.compare_digest(supplied, expected):
        return "invalid session token"
    return None


def authorize(*, method: str, path: str, headers, port: int, token: str) -> str | None:
    """The single gate every request passes through.

    Ordered cheapest-and-most-structural first: a rebinding attempt is rejected
    before we even look at the token, so a probe cannot use the response to
    distinguish "wrong token" from "wrong host".
    """
    err = check_host(headers.get("Host"), port)
    if err:
        return err
    err = check_origin(headers.get("Origin"), port)
    if err:
        return err

    # The static tree is unauthenticated, and has to be: a <link> or a
    # <script src> cannot set a custom header, so requiring the token here
    # would mean the page could never load its own stylesheet.
    #
    # That is safe only because these files carry no data. They are byte for
    # byte the files published in the public repository — an empty frame plus
    # the code that fetches everything else. Every one of those fetches is
    # authenticated. Nothing about this person is in them, and in particular
    # the token is not: it arrives in the URL fragment instead, which never
    # reaches the server at all.
    if method == "GET" and not path.startswith("/api/"):
        return None

    return check_token(headers.get(TOKEN_HEADER), token)
