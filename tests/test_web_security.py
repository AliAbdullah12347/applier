"""Security tests for the local GUI server.

The GUI is the largest attack surface this project has ever had. It serves,
over HTTP, a profile containing a date of birth, a home address, a passport
nationality and an immigration status — and it can start a browser that
submits applications in a real person's name.

"It's only on localhost" is not a defence. A page on any website you have open
can send requests to 127.0.0.1, and with a DNS rebind it can read the replies
too. So the checks below are the actual boundary, and they are tested rather
than asserted in a comment.

Each test names the attack it prevents. If one of these starts failing, the
correct response is to stop, not to adjust the test.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from applier.web import security as S


# --------------------------------------------------------------------------- #
# token
# --------------------------------------------------------------------------- #
def test_token_is_long_and_unpredictable():
    a, b = S.new_token(), S.new_token()
    assert a != b
    assert len(a) >= 40, "a short token is brute-forceable by a local process"


@pytest.mark.parametrize("supplied", [None, "", "wrong", "x" * 43])
def test_bad_tokens_are_refused(supplied):
    assert S.check_token(supplied, "the-real-token") is not None


def test_correct_token_passes():
    tok = S.new_token()
    assert S.check_token(tok, tok) is None


# --------------------------------------------------------------------------- #
# DNS rebinding — the attack that Origin checks do NOT stop
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", [
    "evil.com", "evil.com:8765", "attacker.test:8765",
    "applier.localhost.evil.com:8765",      # suffix trick
    "127.0.0.1.evil.com:8765",              # prefix trick
    "0.0.0.0:8765",                         # not loopback
    "192.168.1.14:8765",                    # LAN address
    "[::ffff:169.254.1.1]:8765",
])
def test_rebinding_hosts_are_refused(host):
    assert S.check_host(host, 8765) is not None, f"{host!r} must be refused"


@pytest.mark.parametrize("host", ["127.0.0.1:8765", "localhost:8765", "[::1]:8765"])
def test_loopback_hosts_are_accepted(host):
    assert S.check_host(host, 8765) is None


def test_host_on_a_different_port_is_refused():
    """A rebind that reaches us on a port we did not open is still a rebind."""
    assert S.check_host("127.0.0.1:9999", 8765) is not None


def test_missing_host_is_refused():
    assert S.check_host(None, 8765) is not None


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origin", [
    "http://evil.com", "https://evil.com", "http://evil.com:8765",
    "http://localhost.evil.com:8765", "file://", "http://127.0.0.1:9999",
])
def test_foreign_origins_are_refused(origin):
    assert S.check_origin(origin, 8765) is not None


@pytest.mark.parametrize("origin", [None, "", "null",
                                    "http://127.0.0.1:8765", "http://localhost:8765"])
def test_own_and_absent_origins_are_allowed(origin):
    assert S.check_origin(origin, 8765) is None


# --------------------------------------------------------------------------- #
# the combined gate
# --------------------------------------------------------------------------- #
class H(dict):
    """Minimal stand-in for an email.Message headers object."""
    def get(self, k, default=None):
        return dict.get(self, k, default)


def test_api_requires_the_token_even_from_loopback():
    err = S.authorize(method="GET", path="/api/state",
                      headers=H({"Host": "127.0.0.1:8765"}), port=8765, token="T")
    assert err is not None


def test_shell_html_loads_without_a_token():
    """The one exception: it holds no data and must load before any script runs."""
    assert S.authorize(method="GET", path="/",
                       headers=H({"Host": "127.0.0.1:8765"}), port=8765, token="T") is None


def test_rebinding_is_refused_before_the_token_is_checked():
    """A prober must not be able to tell 'wrong token' from 'wrong host'."""
    err = S.authorize(method="GET", path="/api/profile",
                      headers=H({"Host": "evil.com:8765", "X-Applier-Token": "T"}),
                      port=8765, token="T")
    assert err is not None and "rebinding" in err


def test_a_valid_request_passes():
    assert S.authorize(method="GET", path="/api/state",
                       headers=H({"Host": "localhost:8765",
                                  "Origin": "http://localhost:8765",
                                  "X-Applier-Token": "T"}),
                       port=8765, token="T") is None


# --------------------------------------------------------------------------- #
# binding
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_refuses_to_bind_a_non_loopback_address(host):
    from applier.web.server import GuiServer
    with pytest.raises(ValueError, match="(?i)loopback"):
        GuiServer(host=host)


def test_csp_allows_no_remote_origins():
    """An injected string must not be able to phone home."""
    for token in ("http://", "https://", "*", "unsafe-inline", "unsafe-eval"):
        assert token not in S.CSP, f"CSP must not contain {token!r}"
    assert "frame-ancestors 'none'" in S.CSP


def test_no_cors_headers_are_ever_sent():
    """With no CORS, every cross-origin preflight fails and the custom
    token header becomes unsettable from a foreign page."""
    for k in S.SECURITY_HEADERS:
        assert not k.lower().startswith("access-control-")


# --------------------------------------------------------------------------- #
# live server: the end-to-end version of the above
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def live():
    from applier.web.server import GuiServer
    srv = GuiServer(port=8899)
    t = threading.Thread(target=lambda: srv.serve_forever(open_browser=False), daemon=True)
    t.start()
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{srv.url}index.html", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    yield srv
    srv.shutdown()


def _req(srv, path, *, token=None, host=None, origin=None, method="GET"):
    r = urllib.request.Request(f"{srv.url.rstrip('/')}{path}", method=method)
    if token:
        r.add_header(S.TOKEN_HEADER, token)
    if host:
        r.add_header("Host", host)
    if origin:
        r.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_live_api_without_token_is_403(live):
    status, _ = _req(live, "/api/state")
    assert status == 403


def test_live_api_with_token_works(live):
    status, body = _req(live, "/api/paths", token=live.token)
    assert status == 200
    assert "paths" in json.loads(body)


def test_live_foreign_origin_is_403(live):
    status, _ = _req(live, "/api/state", token=live.token, origin="http://evil.com")
    assert status == 403


def test_live_rebinding_host_is_403(live):
    status, _ = _req(live, "/api/state", token=live.token, host="evil.com:8899")
    assert status == 403


def test_live_error_body_does_not_say_why(live):
    """A prober learns nothing from the reply."""
    _, body = _req(live, "/api/state", token="wrong")
    assert json.loads(body) == {"error": "refused"}


def test_live_no_secret_endpoint_returns_a_value(live):
    """There is no route that hands back a stored key. Guard that."""
    status, body = _req(live, "/api/secrets", token=live.token)
    assert status == 200
    for s in json.loads(body)["secrets"]:
        assert set(s) <= {"name", "set", "hint"}
        if s.get("hint"):
            assert len(s["hint"]) <= 5, "the hint must be a last-4 suffix, not a key"


def test_live_artifact_path_traversal_is_refused(live):
    for attempt in ("../../config/profile.yaml",
                    "..\\..\\config\\profile.yaml",
                    "/etc/passwd",
                    "....//....//config/profile.yaml"):
        status, _ = _req(live, f"/api/artifact?rel={urllib.parse.quote(attempt)}",
                         token=live.token)
        assert status in (403, 404, 415), f"{attempt!r} returned {status}"


def test_live_security_headers_are_present(live):
    r = urllib.request.Request(f"{live.url}index.html")
    with urllib.request.urlopen(r, timeout=5) as resp:
        for k in ("Content-Security-Policy", "X-Content-Type-Options",
                  "X-Frame-Options", "Referrer-Policy"):
            assert resp.headers.get(k), f"missing {k}"
        assert resp.headers.get("Access-Control-Allow-Origin") is None


def test_live_options_gets_no_cors(live):
    status, _ = _req(live, "/api/state", token=live.token, method="OPTIONS")
    assert status == 405
