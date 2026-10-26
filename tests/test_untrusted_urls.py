"""Links inside a job description are written by whoever posted the job.

Everything else this system reads is published by an employer at a URL the
employer chose. The outreach path is different: it follows links found in the
description text, which makes the URL attacker-controlled. A program on a
laptop fetching an attacker's URL can reach things the open internet cannot --
the router's admin page, a colleague's dev server, and this application's own
GUI on 127.0.0.1.

The checks run after DNS resolution and again after every redirect, because a
hostname is a string and `evil.example.com` resolving to 127.0.0.1 is the
first thing anyone tries.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from applier.net import MAX_BYTES, guarded_get, guarded_text, is_public_url


# --------------------------------------------------------------------------- #
# what is refused
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/",                      # this app's own GUI
    "http://127.0.0.1:8765/api/state",
    "http://localhost:8765/",
    "http://[::1]:8765/",
    "http://0.0.0.0/",
    "http://169.254.169.254/latest/meta-data/",    # cloud credentials
    "http://192.168.1.1/",                         # a home router
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://100.64.0.1/",                          # carrier-grade NAT
])
def test_private_and_loopback_addresses_are_refused(url):
    assert is_public_url(url) is False


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "file://C:/Windows/win.ini",
    "javascript:alert(1)",
    "data:text/html,<script>1</script>",
    "ftp://example.com/",
    "gopher://example.com/",
    "",
    "not a url",
    "http://",
])
def test_only_http_is_fetched(url):
    assert is_public_url(url) is False


def test_a_hostname_that_does_not_resolve_is_refused():
    assert is_public_url("https://nx-" + "z" * 40 + ".invalid/") is False


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com/x?y=1"])
def test_a_public_url_is_allowed(url):
    assert is_public_url(url) is True


# --------------------------------------------------------------------------- #
# redirects
# --------------------------------------------------------------------------- #
class _Redirector(BaseHTTPRequestHandler):
    target = "http://127.0.0.1:1/"
    hits: list[str] = []

    def do_GET(self):                                 # noqa: N802
        type(self).hits.append(self.path)
        if self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", type(self).target)
            self.end_headers()
            return
        body = b"x" * 10_000_000              # bigger than the cap
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):                        # noqa: D102
        pass


@pytest.fixture
def server():
    _Redirector.hits = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_port
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_guard_refuses_the_test_server_itself(server):
    """It is on loopback, so nothing here should reach it at all."""
    assert guarded_text(f"http://127.0.0.1:{server}/") == ""
    assert _Redirector.hits == [], "a loopback request was actually made"


def test_a_response_larger_than_the_cap_is_truncated(monkeypatch, server):
    """A hostile server must not be able to exhaust memory through this."""
    monkeypatch.setattr("applier.net.is_public_url", lambda _u: True)
    body = guarded_text(f"http://127.0.0.1:{server}/big")
    assert 0 < len(body) <= MAX_BYTES + 1


def test_a_redirect_into_the_private_network_is_not_followed(monkeypatch, server):
    """httpx's own follow_redirects checks the first URL and nothing after."""
    monkeypatch.setattr("applier.net.is_public_url",
                        lambda u: "127.0.0.1:1" not in u)
    _Redirector.target = "http://127.0.0.1:1/secret"
    assert guarded_get(f"http://127.0.0.1:{server}/redirect") is None


def test_a_redirect_loop_terminates(monkeypatch, server):
    monkeypatch.setattr("applier.net.is_public_url", lambda _u: True)
    _Redirector.target = f"http://127.0.0.1:{server}/redirect"
    assert guarded_get(f"http://127.0.0.1:{server}/redirect") is None


# --------------------------------------------------------------------------- #
# the paths that actually use it
# --------------------------------------------------------------------------- #
def test_a_hostile_job_description_cannot_make_the_app_call_itself():
    """The scenario, end to end.

    A posting whose description links to this machine's own GUI. The employer
    domain resolver reads those links, so without the guard it would fetch
    them -- and a 200 from the static tree would then be parsed as a company
    website.
    """
    from applier.outreach.forjob import _domains_in, employer_domain

    hostile = (
        "Great opportunity! See http://127.0.0.1:8765/api/state and "
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/ "
        "and mail us at careers@192.168.1.1"
    )
    job = {"company": "Hostile", "title": "Intern",
           "url": "https://job-boards.greenhouse.io/hostile/jobs/1",
           "description": hostile}

    # Two independent reasons nothing happens, and both are worth keeping.
    # The link extractor requires a real TLD, so a bare address is never even
    # treated as a domain...
    assert _domains_in(hostile) == []
    # ...and the guard would refuse it regardless, which is the reason that
    # does not depend on an accident of a regular expression.
    assert employer_domain(job, None, None) == ""


def test_every_outreach_fetch_goes_through_the_guard():
    """One way in, so a new call site cannot quietly reopen this."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "applier" / "outreach"

    # forjob.py reads URLs out of job descriptions, so it may not build a
    # client at all.
    src = (root / "forjob.py").read_text(encoding="utf-8")
    assert "httpx.Client(" not in src, "forjob.py builds a raw client"
    assert "guarded_text" in src or "guarded_get" in src

    # discover.py keeps exactly two raw clients, both against fixed hosts that
    # no caller can influence: api.github.com and api.hunter.io. A third would
    # mean something new is fetching a URL from somewhere, which is the thing
    # this test is here to notice.
    src = (root / "discover.py").read_text(encoding="utf-8")
    clients = src.count("httpx.Client(")
    assert clients <= 2, f"discover.py grew a new raw client ({clients} now)"
    for fixed in ("api.github.com", "api.hunter.io"):
        assert fixed in src, f"the {fixed} client is gone; recount the exemption"
    assert "guarded_text" in src, "from_team_page is no longer guarded"


def test_the_browser_is_not_opened_on_a_private_url():
    from applier.outreach.people import fetch_rendered
    assert fetch_rendered("http://127.0.0.1:8765/") == ""
    assert fetch_rendered("file:///etc/passwd") == ""
