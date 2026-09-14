"""Browser session management.

Runs a real, visible Chrome on your own machine with a persistent profile.

That is a deliberate constraint, not laziness. Greenhouse ships IPQS-backed
fraud detection that flags data-centre IPs and timezone/location mismatches as
fraud signals. Running this on a cloud VM, in GitHub Actions, behind a VPN, or
headless is the fastest way to have applications silently binned or an account
flagged. So: headed browser, residential connection, OS timezone matching where
you actually are.

The persistent profile matters too — employer Workday tenants each need their own
account, and keeping the session means you log in once per company, not once per
application.
"""

from __future__ import annotations

import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..config import Config


class BrowserUnavailable(RuntimeError):
    pass


class Session:
    """Thin wrapper over a Playwright persistent context."""

    def __init__(self, settings: Config) -> None:
        self.s = settings
        self._pw = None
        self._ctx = None
        self.page = None

    # ------------------------------------------------------------------ #
    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BrowserUnavailable(
                "Playwright is not installed.\n"
                "  pip install playwright\n"
                "  python -m playwright install chromium"
            ) from e

        headless = bool(self.s.get("apply.headless", False))
        if headless:
            # Loud, because this silently degrades application quality.
            print("WARNING: headless=true. Greenhouse fraud detection flags headless "
                  "and datacenter signals. Strongly prefer headless=false.")

        profile_dir = Path(self.s.get("apply.profile_dir", ".browser-profile")).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "timezone_id": self.s.get("meta.timezone", "America/New_York"),
            "locale": "en-US",
            "viewport": {"width": 1440, "height": 900},
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        channel = self.s.get("apply.browser_channel", "chrome")
        if channel:
            launch["channel"] = channel
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch)
        except Exception:
            launch.pop("channel", None)          # fall back to bundled chromium
            self._ctx = self._pw.chromium.launch_persistent_context(**launch)

        self._ctx.set_default_timeout(45_000)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self.page

    # ------------------------------------------------------------------ #
    def goto(self, url: str, *, wait: str = "domcontentloaded"):
        assert self.page is not None
        self.page.goto(url, wait_until=wait)
        self.settle()
        return self.page

    def settle(self, extra_ms: int = 0) -> None:
        """Let SPA frameworks finish rendering, then pause a human beat."""
        assert self.page is not None
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self.human_pause(extra_ms)

    def human_pause(self, extra_ms: int = 0) -> None:
        lo, hi = self.s.get("apply.human_delay_ms", [400, 1800])
        time.sleep((random.randint(int(lo), int(hi)) + extra_ms) / 1000.0)

    def screenshot(self, path: Path) -> Path | None:
        if not self.s.get("apply.screenshot_every_step", True) or self.page is None:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            return None

    def upload(self, selector: str, file_path: Path) -> bool:
        assert self.page is not None
        try:
            self.page.set_input_files(selector, str(file_path))
            self.human_pause()
            return True
        except Exception:
            return False

    def click(self, selector: str) -> bool:
        assert self.page is not None
        try:
            self.page.click(selector, timeout=15_000)
            self.settle()
            return True
        except Exception:
            return False

    def close(self) -> None:
        for obj in (self._ctx, self._pw):
            try:
                if obj is not None:
                    obj.close() if obj is self._ctx else obj.stop()
            except Exception:
                pass
        self._ctx = self._pw = self.page = None


@contextmanager
def session(settings: Config) -> Iterator[Session]:
    s = Session(settings)
    try:
        s.start()
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
def detect_ats(url: str, html: str = "") -> str:
    """Identify the platform so a fast-path adapter can run instead of inference."""
    u = (url or "").lower()
    h = (html or "").lower()
    table = [
        ("greenhouse", ["greenhouse.io", "boards.greenhouse", "grnhse"]),
        ("lever", ["jobs.lever.co", "lever.co"]),
        ("ashby", ["ashbyhq.com", "jobs.ashbyhq"]),
        ("workday", ["myworkdayjobs.com", "workday", "wd1.", "wd3.", "wd5."]),
        ("smartrecruiters", ["smartrecruiters.com"]),
        ("icims", ["icims.com"]),
        ("workable", ["workable.com"]),
        ("recruitee", ["recruitee.com"]),
        ("teamtailor", ["teamtailor.com"]),
        ("bamboohr", ["bamboohr.com"]),
        ("jobvite", ["jobvite.com"]),
        ("taleo", ["taleo.net"]),
        ("successfactors", ["successfactors.com"]),
        ("oraclecloud", ["oraclecloud.com", "fa.oraclecloud"]),
    ]
    for name, needles in table:
        if any(n in u for n in needles) or any(n in h for n in needles):
            return name
    return "unknown"
