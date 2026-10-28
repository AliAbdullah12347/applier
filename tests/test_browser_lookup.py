"""Finding the browser, and saying so when it cannot be found.

Inside a frozen build Playwright's driver computes the wrong executable
path -- the Linux shape, missing the platform subdirectory and the `.exe` --
and reports a working install as a missing one. `applier.browsers` looks for
the file itself and hands over an explicit `executable_path`.

The failure message used to quote the path the *driver* computed and say
nothing about the path this program searched. When those two disagree, which
is the entire reason this module exists, the report describes a file nobody
looked for; and the remedy it offered was a terminal command from a library
the reader never installed. Both halves are fixed here: the message names
what was searched, and the program can perform the install itself.
"""

from __future__ import annotations

import sys

import pytest

from applier import browsers


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(browsers, "browsers_root", lambda: tmp_path)
    return tmp_path


def _make(root, build: str, folder: str, name: str) -> None:
    d = root / build / folder
    d.mkdir(parents=True, exist_ok=True)
    exe = d / name
    exe.write_bytes(b"MZ")
    if sys.platform != "win32":
        exe.chmod(0o755)


def _exe(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


# --------------------------------------------------------------------------- #
# what counts as a browser
# --------------------------------------------------------------------------- #
def test_the_headless_shell_is_found(fake_root):
    _make(fake_root, "chromium_headless_shell-1234",
          "chrome-headless-shell-win64", _exe("chrome-headless-shell"))
    assert browsers.find_chromium() is not None


def test_a_helper_beside_the_browser_is_not_the_browser(fake_root):
    """`chrome_proxy.exe` and `chrome_pwa_launcher.exe` sit in the same
    directory and are not browsers.

    The glob has to be loose about the suffix, because the file is `chrome`
    on Linux and `chrome.exe` on Windows -- which also made it match these.
    Handing one to Playwright as `executable_path` fails in a way that looks
    exactly like a missing install.
    """
    _make(fake_root, "chromium-1234", "chrome-win64", _exe("chrome_proxy"))
    _make(fake_root, "chromium-1234", "chrome-win64", _exe("chrome_pwa_launcher"))
    assert browsers.find_chromium() is None

    _make(fake_root, "chromium-1234", "chrome-win64", _exe("chrome"))
    assert browsers.find_chromium().stem == "chrome"


def test_the_newest_build_wins_by_number_not_by_text(fake_root):
    """`chromium-999` sorts above `chromium-1234` as text.

    Playwright leaves old builds in place on upgrade and the driver expects
    the one matching its own version, so picking the wrong one is picking a
    browser that will not talk to the driver.
    """
    for build in ("chromium-999", "chromium-1234"):
        _make(fake_root, build, "chrome-win64", _exe("chrome"))
    assert "1234" in str(browsers.find_chromium())


def test_the_headless_shell_is_preferred_over_the_full_browser(fake_root):
    _make(fake_root, "chromium-1234", "chrome-win64", _exe("chrome"))
    _make(fake_root, "chromium_headless_shell-1234",
          "chrome-headless-shell-win64", _exe("chrome-headless-shell"))
    assert "headless" in str(browsers.find_chromium())


def test_nothing_installed_is_not_an_error(fake_root):
    assert browsers.find_chromium() is None
    assert browsers.launch_kwargs() == {}


def test_launch_kwargs_names_the_executable_when_there_is_one(fake_root):
    _make(fake_root, "chromium_headless_shell-1234",
          "chrome-headless-shell-win64", _exe("chrome-headless-shell"))
    assert "executable_path" in browsers.launch_kwargs()


# --------------------------------------------------------------------------- #
# saying what was searched
# --------------------------------------------------------------------------- #
def test_diagnose_reports_the_root_it_searched(fake_root):
    d = browsers.diagnose()
    assert d["root"] == str(fake_root)
    assert d["root_exists"] is True


def test_a_missing_directory_is_reported_as_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(browsers, "browsers_root", lambda: tmp_path / "nope")
    assert "no browser directory" in browsers.describe_failure("whatever")


def test_a_directory_with_no_browser_says_what_is_in_it(fake_root):
    (fake_root / "ffmpeg-1011").mkdir()
    message = browsers.describe_failure("Executable doesn't exist at ...")
    assert "ffmpeg-1011" in message


def test_a_browser_that_was_found_and_still_failed_says_so(fake_root):
    _make(fake_root, "chromium_headless_shell-1234",
          "chrome-headless-shell-win64", _exe("chrome-headless-shell"))
    message = browsers.describe_failure("some driver complaint")
    assert "was found at" in message and "some driver complaint" in message


def test_the_failure_message_never_quotes_only_the_drivers_guess(fake_root):
    """The regression this whole module exists to prevent.

    The driver's computed path is the Linux shape of a Windows install. A
    report that quotes only that sends someone looking for a file that was
    never going to be there.
    """
    _make(fake_root, "chromium_headless_shell-1234",
          "chrome-headless-shell-win64", _exe("chrome-headless-shell"))
    guess = r"Executable doesn't exist at ...\chromium_headless_shell-1234\chrome-headless-shell"
    assert str(fake_root) in browsers.describe_failure(guess)


# --------------------------------------------------------------------------- #
# installing it
# --------------------------------------------------------------------------- #
def test_install_puts_the_browser_where_this_program_looks(fake_root, monkeypatch):
    """The two paths were allowed to differ, which is one way a browser gets
    installed and still reported missing."""
    seen: dict = {}

    def fake_run(cmd, env=None, **kw):
        seen["env"] = env
        _make(fake_root, "chromium_headless_shell-1234",
              "chrome-headless-shell-win64", _exe("chrome-headless-shell"))

        class R:
            returncode, stdout, stderr = 0, "done", ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, message = browsers.install()
    assert ok, message
    assert seen["env"]["PLAYWRIGHT_BROWSERS_PATH"] == str(fake_root)


def test_install_that_reports_success_but_produces_nothing_is_a_failure(
        fake_root, monkeypatch):
    def fake_run(cmd, env=None, **kw):
        class R:
            returncode, stdout, stderr = 0, "all good", ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, message = browsers.install()
    assert not ok
    assert "nothing runnable appeared" in message


def test_a_failed_install_returns_its_output_rather_than_raising(
        fake_root, monkeypatch):
    def fake_run(cmd, env=None, **kw):
        class R:
            returncode, stdout, stderr = 1, "", "network unreachable"
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, message = browsers.install()
    assert not ok and "network unreachable" in message
