"""The Chrome extension, and the one security rule it changed.

The extension needed exactly one concession: a `chrome-extension://` origin is
allowed past the Origin check. That is defensible because a web page cannot
forge such an origin — only the browser sets it, and only for a real installed
extension — and because the token is still required afterwards, so a hostile
extension without it gets precisely as far as a hostile web page does.

Everything else stays: no CORS headers, so a page still cannot read a reply;
loopback-only binding; the same token on every call.

These tests exist so that concession cannot quietly widen into "allow any
origin", which is the shape this kind of exception usually decays into.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from applier.web import security as S

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"


# --------------------------------------------------------------------------- #
# the origin concession, and its limits
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("origin", [
    "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
    "moz-extension://11111111-2222-3333-4444-555555555555",
])
def test_an_extension_origin_is_allowed_past_the_origin_check(origin):
    assert S.check_origin(origin, 8765) is None


def test_an_extension_origin_still_needs_the_token():
    """The concession is to the Origin check only. The token is the authority."""
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    err = S.authorize(
        method="POST", path="/api/apply",
        headers=H({"Host": "127.0.0.1:8765",
                   "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}),
        port=8765, token="the-real-token")
    assert err is not None, "an extension reached the API without a token"


@pytest.mark.parametrize("origin", [
    "http://evil.com",
    "https://evil.com",
    "http://localhost.evil.com:8765",
    "chrome-extension-evil://abc",
    "javascript://abc",
    "data://abc",
    "file://abc",
])
def test_the_concession_did_not_widen_to_everything(origin):
    """Only the two real extension schemes. Not anything that merely looks like one."""
    assert S.check_origin(origin, 8765) is not None, f"{origin!r} was allowed"


def test_still_no_cors_headers():
    """A web page must still be unable to READ a reply, extension or not."""
    for k in S.SECURITY_HEADERS:
        assert not k.lower().startswith("access-control-")


def test_rebinding_is_still_refused_for_an_extension_origin():
    """The Host check is independent and must still fire."""
    class H(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    err = S.authorize(
        method="GET", path="/api/profile",
        headers=H({"Host": "evil.com:8765",
                   "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                   "X-Applier-Token": "T"}),
        port=8765, token="T")
    assert err is not None and "rebinding" in err


# --------------------------------------------------------------------------- #
# the extension itself
# --------------------------------------------------------------------------- #
def test_the_extension_ships_every_file_its_manifest_names():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    referenced = {
        manifest["background"]["service_worker"],
        manifest["action"]["default_popup"],
        manifest["options_page"],
        *manifest["icons"].values(),
    }
    for name in referenced:
        assert (EXT / name).is_file(), f"manifest names {name}, which is missing"


def test_every_local_file_the_pages_reference_exists():
    for page in ("popup.html", "options.html"):
        html = (EXT / page).read_text(encoding="utf-8")
        for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
            if ref.startswith(("http:", "https:", "data:", "#")):
                continue
            assert (EXT / ref).is_file(), f"{page} references missing {ref}"


def test_the_extension_talks_to_nowhere_but_this_machine():
    """No analytics, no CDN, no remote anything. The whole point is locality."""
    for f in EXT.glob("*"):
        if f.suffix not in (".js", ".html", ".css", ".json"):
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*", "<!--")):
                continue
            for url in re.findall(r"https?://[\w.-]+", line):
                host = url.split("//", 1)[1]
                assert host.startswith(("127.0.0.1", "localhost")), \
                    f"{f.name} references a remote host: {url}"


def test_host_permissions_are_loopback_only():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    for perm in manifest["host_permissions"]:
        assert perm.startswith(("http://127.0.0.1", "http://localhost")), \
            f"host permission reaches beyond this machine: {perm}"


def test_the_extension_asks_for_no_broad_permissions():
    """<all_urls> or a tabs permission would let it read every page you open."""
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    forbidden = {"<all_urls>", "tabs", "webRequest", "cookies", "history",
                 "browsingData", "debugger", "management", "proxy"}
    granted = set(manifest.get("permissions", []))
    overlap = granted & forbidden
    assert not overlap, f"over-broad permissions requested: {sorted(overlap)}"
    assert "activeTab" in granted, "needs activeTab to read the posting you are on"


def test_the_extension_csp_blocks_remote_script():
    manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    csp = manifest["content_security_policy"]["extension_pages"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_no_extension_file_uses_a_markup_sink():
    """Job titles come from employer pages. Same rule as the web UI."""
    for f in EXT.glob("*.js"):
        for line in f.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*")):
                continue
            for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML",
                         "document.write", "eval(", "new Function"):
                assert sink not in line, f"{f.name} uses {sink}: {stripped[:70]}"


def test_the_token_is_never_logged():
    """A console.log of the token would park it in the devtools history."""
    for f in EXT.glob("*.js"):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(r"console\.\w+\(([^)]*)\)", src):
            assert "token" not in m.group(1).lower(), \
                f"{f.name} logs something token-shaped: {m.group(0)[:60]}"
