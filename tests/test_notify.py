"""The one thing in this program that sends anything to anyone.

Outreach drafts messages and stops. This sends one kind of mail, to one
address, about one event: an application submitted in your name while you
were not watching.

Three properties, and the first is the one that matters:

* **It cannot fail the thing it reports on.** A submission is written to the
  database the instant the click returns, because an employer already has
  the form and a second submission is the worst thing this system can do.
  Everything after that point is housekeeping. A mail server that is down,
  slow or rejecting a password is a fact about a mail server, and if that
  could propagate the run would mark a sent application failed and apply
  again tomorrow.

* **The password is never in a file.** It is read from the keychain by the
  name `notify.email.password_env` holds, and nothing returns it.

* **Employer text never reaches a mail header.** Company and title come from
  a posting, which is written by someone else; a newline in a subject line
  is how headers get injected.
"""

from __future__ import annotations

import base64
import smtplib
import socket
import threading

import pytest

from applier import notify


class Cfg(dict):
    """The duck-typed shape `Config.get` presents, dotted keys and all."""

    def get(self, key, default=None):          # noqa: A003
        node = self
        for part in str(key).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def settings(**over):
    email = {"enabled": True, "to": "me@example.com", "host": "smtp.example.com",
             "port": 587, "username": "", "password_env": "TEST_SMTP_PASSWORD",
             "starttls": True}
    email.update(over)
    return Cfg({"notify": {"email": email}})


PROFILE = Cfg({"identity": {"email": "fallback@example.com"}})


class _FakeSMTP:
    """Enough of the protocol to accept one authenticated message.

    `smtpd` left the standard library in 3.12 and a mail server is too much
    to pull in for one test. This speaks EHLO, AUTH LOGIN, the envelope and
    DATA, and records what actually arrived on the socket.
    """

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))        # loopback, ephemeral port
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[str] = []
        self.auth: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        f = conn.makefile("rwb")
        conn.sendall(b"220 fake ESMTP\r\n")
        data_mode, body = False, []
        while True:
            line = f.readline()
            if not line:
                break
            if data_mode:
                if line.strip() == b".":
                    data_mode = False
                    self.received.append(b"".join(body).decode("utf-8", "replace"))
                    conn.sendall(b"250 OK queued\r\n")
                else:
                    body.append(line)
                continue
            cmd = line.decode("ascii", "replace").strip()
            up = cmd.upper()
            if up.startswith(("EHLO", "HELO")):
                conn.sendall(b"250-fake\r\n250 AUTH LOGIN PLAIN\r\n")
            elif up.startswith("AUTH LOGIN"):
                conn.sendall(b"334 VXNlcm5hbWU6\r\n")
                self.auth.append(base64.b64decode(f.readline().strip()).decode())
                conn.sendall(b"334 UGFzc3dvcmQ6\r\n")
                self.auth.append(base64.b64decode(f.readline().strip()).decode())
                conn.sendall(b"235 authenticated\r\n")
            elif up.startswith("AUTH PLAIN"):
                parts = cmd.split(" ", 2)
                raw = parts[2] if len(parts) > 2 else f.readline().strip().decode()
                self.auth.extend(base64.b64decode(raw).decode().split("\x00")[1:])
                conn.sendall(b"235 authenticated\r\n")
            elif up.startswith(("MAIL FROM", "RCPT TO")):
                conn.sendall(b"250 OK\r\n")
            elif up == "DATA":
                data_mode, body = True, []
                conn.sendall(b"354 go ahead\r\n")
            elif up == "QUIT":
                conn.sendall(b"221 bye\r\n")
                break
            else:
                conn.sendall(b"250 OK\r\n")
        conn.close()


@pytest.fixture
def fake_smtp():
    return _FakeSMTP()


@pytest.fixture
def password(monkeypatch):
    monkeypatch.setenv("TEST_SMTP_PASSWORD", "app-password")


# --------------------------------------------------------------------------- #
# it cannot fail the thing it reports on
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("boom", [
    smtplib.SMTPAuthenticationError(535, b"nope"),
    smtplib.SMTPServerDisconnected("closed"),
    OSError("network is down"),
    TimeoutError("timed out"),
    RuntimeError("something nobody predicted"),
])
def test_a_failing_mail_server_is_not_a_failed_application(boom, monkeypatch, password):
    monkeypatch.setattr(notify, "send",
                        lambda *a, **k: (_ for _ in ()).throw(boom))
    out = notify.on_submission(settings(), PROFILE, {"company": "X", "title": "Y"})
    assert "failed" in out, "the caller logs this string; it has to say what happened"


def test_an_unconfigured_notifier_is_not_an_error():
    out = notify.on_submission(Cfg({}), PROFILE, {"company": "X", "title": "Y"})
    assert out == "email notification not configured"


def test_a_missing_password_does_not_raise_at_submission_time():
    out = notify.on_submission(settings(), PROFILE, {"company": "X", "title": "Y"})
    assert "not configured" in out


def test_the_submission_hook_runs_only_after_the_row_is_written():
    """Ordering, read off the source, because the cost of getting it wrong is
    an application sent twice and no test can observe that after the fact."""
    import inspect

    from applier import pipeline

    src = inspect.getsource(pipeline._execute_application)
    update = src.index("UPDATE applications SET status=CASE WHEN status='submitted'")
    hook = src.index("notify.on_submission")
    assert update < hook, (
        "the notification must come after the applications row is written; "
        "anything between the click and that write can cost a double submission")


# --------------------------------------------------------------------------- #
# the password
# --------------------------------------------------------------------------- #
def test_no_password_means_not_configured_rather_than_a_half_send():
    assert notify.email_config(settings(), PROFILE) is None


def test_the_password_is_read_from_the_named_variable(password):
    cfg = notify.email_config(settings(), PROFILE)
    assert cfg is not None and cfg.password == "app-password"


def test_describe_never_returns_the_password(password):
    state = notify.describe(settings(), PROFILE)
    assert "app-password" not in repr(state)
    assert state["password_set"] is True


# --------------------------------------------------------------------------- #
# employer text never reaches a header
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("company", [
    "Evil\nBcc: victim@example.com",
    "Evil\r\nSubject: something else",
    "Evil\x00Corp",
])
def test_a_posting_cannot_inject_a_mail_header(company):
    subject, _body = notify.submission_message({"company": company, "title": "Dev"})
    assert "\n" not in subject and "\r" not in subject and "\x00" not in subject


def test_a_very_long_title_does_not_run_away_with_the_subject():
    subject, _ = notify.submission_message(
        {"company": "C", "title": "Engineer " * 100})
    assert len(subject) < 200


def test_the_body_carries_what_you_would_need_to_check_it():
    _subject, body = notify.submission_message(
        {"company": "Databricks", "title": "SWE Intern", "location": "Remote"},
        form_url="https://boards.example.com/apply",
        final_url="https://boards.example.com/thanks",
        resume="Resume.pdf", resume_sha="a" * 64, fields=12)
    for needle in ("Databricks", "SWE Intern", "Remote",
                   "https://boards.example.com/apply",
                   "https://boards.example.com/thanks", "12", "a" * 16):
        assert needle in body, needle


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def test_the_old_boolean_setting_means_off():
    """`notify.email` held a bool before it held a block.

    An old settings file is not a configuration error -- it is a
    configuration written before the feature existed, and it means off.
    """
    assert notify.email_config(Cfg({"notify": {"email": False}}), PROFILE) is None
    assert notify.describe(Cfg({"notify": {"email": False}}), PROFILE)["ready"] is False


def test_a_blank_address_falls_back_to_the_profile(password):
    cfg = notify.email_config(settings(to=""), PROFILE)
    assert cfg is not None and cfg.to == "fallback@example.com"


def test_a_blank_username_falls_back_to_the_address(password):
    cfg = notify.email_config(settings(username=""), PROFILE)
    assert cfg is not None and cfg.username == "me@example.com"


def test_port_465_is_implicit_tls(password):
    assert notify.email_config(settings(port=465), PROFILE).implicit_tls
    assert not notify.email_config(settings(port=587), PROFILE).implicit_tls


def test_describe_says_which_thing_is_missing():
    assert "enabled" in notify.describe(settings(enabled=False), PROFILE)["reason"]
    assert "host" in notify.describe(settings(host=""), PROFILE)["reason"]
    assert "TEST_SMTP_PASSWORD" in notify.describe(settings(), PROFILE)["reason"]
    blank = notify.describe(settings(to=""), Cfg({}))["reason"]
    assert "address" in blank


# --------------------------------------------------------------------------- #
# the test path, which a person waits on
# --------------------------------------------------------------------------- #
def test_send_test_raises_so_the_person_sees_why():
    with pytest.raises(notify.NotifyError):
        notify.send_test(settings(enabled=False), PROFILE)


def test_send_test_says_it_is_a_test(monkeypatch, password):
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(notify, "send",
                        lambda cfg, subject, body: sent.append((subject, body)))
    to = notify.send_test(settings(), PROFILE)
    assert to == "me@example.com"
    subject, body = sent[0]
    assert subject.startswith("[test]")
    assert "No application was submitted" in body


def test_a_bad_password_names_the_app_password(monkeypatch, password):
    """Gmail rejecting an account password is the overwhelmingly likely
    failure, and "535" on its own tells nobody what to do about it."""
    class Boom:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): raise smtplib.SMTPAuthenticationError(535, b"denied")
        def send_message(self, m): pass

    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: Boom())
    cfg = notify.email_config(settings(), PROFILE)
    with pytest.raises(notify.NotifyError, match="App Password"):
        notify.send(cfg, "s", "b")


# --------------------------------------------------------------------------- #
# the wire, not the mock
# --------------------------------------------------------------------------- #
def test_a_message_really_goes_out_over_smtp(fake_smtp):
    """smtplib against a socket, because everything above this is a mock.

    The mocked tests say the right functions are called. Only this one says
    the login happens, the message is accepted, and what arrives is what was
    written.
    """
    cfg = notify.EmailConfig(
        to="me@example.com", host="127.0.0.1", port=fake_smtp.port,
        username="me@example.com", password="app-pw", starttls=False)
    subject, body = notify.submission_message(
        {"company": "Databricks", "title": "SWE Intern"},
        form_url="https://boards.example.com/apply", fields=14)
    notify.send(cfg, subject, body)
    fake_smtp.thread.join(timeout=10)

    assert fake_smtp.auth == ["me@example.com", "app-pw"]
    wire = fake_smtp.received[0]
    assert "Subject: Applied: Databricks - SWE Intern" in wire, (
        "an em-dash here forces encoded-word encoding of one character")
    assert "Auto-Submitted: auto-generated" in wire, "or a vacation responder replies"
    assert "boards.example.com/apply" in wire


def test_the_message_is_plain_ascii():
    """It has to survive a Windows console, a log file and a phone preview."""
    subject, body = notify.submission_message(
        {"company": "C", "title": "T"}, form_url="u", resume_sha="f" * 64)
    assert (subject + body).isascii()
