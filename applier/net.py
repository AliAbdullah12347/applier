"""Fetching a URL that came from somewhere untrustworthy.

Most of what this system reads is published by employers, which is fine. The
outreach path is different: it follows links found *inside a job description*,
and a job description is written by whoever posted the job. That makes the URL
attacker-controlled, and an attacker-controlled URL fetched by a program
running on a laptop is a server-side request forgery.

The three things worth stopping:

* **The local machine.** `http://127.0.0.1:8765/` is this application's own
  GUI. The API refuses a request with no token, but nothing should be making
  the attempt, and the static tree answers 200 to anyone.
* **The private network.** `http://192.168.1.1/`, a router's admin page, a
  colleague's dev server. A laptop on a home or campus network can reach
  things the open internet cannot, which is the entire value of the attack.
* **Cloud metadata.** `http://169.254.169.254/` returns credentials on most
  cloud providers. This runs on a laptop today; it should not become a
  vulnerability the day it runs anywhere else.

Checked after DNS resolution rather than on the hostname, because
`evil.example.com` resolving to 127.0.0.1 is the standard way around a
string check, and re-checked after every redirect for the same reason.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

# Generous for a team page, small enough that a hostile server cannot use this
# as a memory exhaustion primitive. The HTML parser downstream is quadratic in
# some pathological inputs.
MAX_BYTES = 4_000_000
MAX_REDIRECTS = 4
DEFAULT_TIMEOUT = 20.0


class BlockedURL(ValueError):
    """The URL points somewhere a job description has no business sending us."""


def _addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def is_public_url(url: str) -> bool:
    """True only for an http(s) URL resolving entirely to public addresses."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False

    # A literal address skips DNS, so check it directly.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [literal] if literal is not None else _addresses(host)
    if not addresses:
        return False

    for addr in addresses:
        # is_global is the complete test -- it is false for every
        # special-purpose range in RFC 6890, including ones that are not
        # "private": 100.64.0.0/10 (carrier-grade NAT) has is_private False
        # and is_global False, and enumerating ranges by hand missed it.
        # The explicit flags stay because they name what is being stopped.
        if not addr.is_global:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
            return False
        # ::ffff:127.0.0.1 and friends.
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None and (mapped.is_private or mapped.is_loopback):
            return False
    return True


def guarded_get(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                headers: dict[str, str] | None = None,
                max_bytes: int = MAX_BYTES) -> httpx.Response | None:
    """GET a URL that came from untrusted text, or None.

    Redirects are followed by hand so that every hop is checked. httpx's own
    `follow_redirects` would resolve the first URL, find it public, and then
    cheerfully follow a 302 to 127.0.0.1.
    """
    seen: set[str] = set()
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if current in seen or not is_public_url(current):
            return None
        seen.add(current)
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False,
                              headers=headers or {}) as client:
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = str(httpx.URL(current).join(location))
                        continue
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            break
                    # iter_bytes has already decompressed the body, so the
                    # transfer headers now describe something that no longer
                    # exists. Carrying them over made httpx decompress the
                    # plain bytes a second time and raise DecodingError --
                    # which is an HTTPError, so it was swallowed and every
                    # gzipped response in the world came back as None.
                    headers = httpx.Headers(
                        [(k, v) for k, v in response.headers.multi_items()
                         if k.lower() not in ("content-encoding", "content-length")])
                    return httpx.Response(
                        status_code=response.status_code,
                        headers=headers,
                        content=bytes(body[:max_bytes]),
                        request=response.request,
                    )
        except (httpx.HTTPError, ValueError, OSError):
            return None
    return None


def guarded_text(url: str, **kw: Any) -> str:
    """The body of a guarded GET, or "" for anything that did not work out."""
    response = guarded_get(url, **kw)
    if response is None or response.status_code >= 400:
        return ""
    try:
        return response.text
    except Exception:                                   # noqa: BLE001
        return ""
