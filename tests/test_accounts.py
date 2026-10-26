"""Credentials the system mints on your behalf.

Workday and its relatives run a separate tenant per employer, each wanting its
own account, so applying means creating one. That is the system acting as the
candidate rather than filling in a form for him, and it has two properties
worth guarding.

It must be idempotent per employer. A second account at the same company is
not a harmless duplicate: it splits the application history, and plenty of
tenants simply refuse a second registration on one address, which turns into
being locked out of a company rather than applying to it.

And the password must never reach the database. The row records where the
secret lives; the secret lives in the OS keychain.

The keychain is stubbed here. A test suite that writes real credentials to the
machine it runs on is its own kind of defect.
"""

from __future__ import annotations

import pytest

from applier.apply import accounts as A
from applier.config import Config
from applier.db import Database


@pytest.fixture
def vault(monkeypatch) -> dict:
    """Stand in for the OS keychain, so the real one is untouched."""
    store: dict[tuple[str, str], str] = {}

    def fake_set(key, value, *, service="applier"):
        store[(service, key)] = value

    def fake_get(key, *, service="applier", required=False):
        return store.get((service, key))

    monkeypatch.setattr(A, "set_secret", fake_set)
    monkeypatch.setattr(A, "get_secret", fake_get)
    return store


@pytest.fixture
def store(tmp_path, vault) -> A.AccountStore:
    db = Database(tmp_path / "t.db")
    settings = Config({"apply": {"accounts": {"password_length": 24}}})
    profile = Config({"identity": {"email": "work@example.com",
                                   "email_personal": "personal@example.com"}})
    return A.AccountStore(db, settings, profile)


# --------------------------------------------------------------------------- #
# passwords
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("_run", range(40))
def test_a_generated_password_always_satisfies_the_usual_policy(_run):
    """Rejected at the registration form is a silent dead end."""
    pw = A.generate_password(24)
    assert len(pw) == 24
    assert any(c.islower() for c in pw)
    assert any(c.isupper() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(c in "!@#$%^&*-_=+" for c in pw)


def test_passwords_are_not_reused():
    assert len({A.generate_password(20) for _ in range(50)}) == 50


# --------------------------------------------------------------------------- #
# one account per employer
# --------------------------------------------------------------------------- #
def test_creating_an_account_twice_returns_the_first_one(store):
    """The property that keeps a second application from locking you out."""
    first = store.create("https://acme.wd1.myworkdayjobs.com/careers", ats="workday")
    second = store.create("https://acme.wd1.myworkdayjobs.com/other-req", ats="workday")

    assert first.password == second.password
    assert first.username == second.username
    assert first.created is True
    assert second.created is False, "a second registration was attempted"
    assert len(store.all()) == 1


def test_the_same_host_in_different_spellings_is_one_employer(store):
    a = store.create("https://www.acme.com/apply")
    b = store.create("https://acme.com/apply/other")
    assert a.password == b.password
    assert len(store.all()) == 1


def test_different_employers_get_different_accounts(store):
    a = store.create("https://acme.com/apply")
    b = store.create("https://other.com/apply")
    assert a.password != b.password
    assert len(store.all()) == 2


# --------------------------------------------------------------------------- #
# where the secret lives
# --------------------------------------------------------------------------- #
def test_the_password_is_never_written_to_the_database(store, vault):
    creds = store.create("https://acme.com/apply")
    rows = store.all()
    assert rows, "nothing was recorded"
    blob = " ".join(str(v) for row in rows for v in row.values())
    assert creds.password not in blob, "the password is sitting in the database"
    assert creds.password in vault.values(), "the password never reached the keychain"


def test_the_row_says_where_to_find_the_secret(store):
    store.create("https://acme.com/apply")
    row = store.all()[0]
    assert row["keyring_service"]
    assert row["keyring_key"]
    assert "acme.com" in row["keyring_key"]


def test_an_account_with_no_stored_password_is_not_returned(store, vault):
    """A keychain entry someone deleted must not read as a usable account."""
    store.create("https://acme.com/apply")
    vault.clear()
    assert store.get("https://acme.com/apply") is None


# --------------------------------------------------------------------------- #
# which address
# --------------------------------------------------------------------------- #
def test_the_personal_address_is_preferred(store):
    """Institutional Workspace admins block third-party apps, which breaks
    the verification email this depends on."""
    assert store.username() == "personal@example.com"


def test_it_falls_back_to_the_only_address_there_is(tmp_path, vault):
    db = Database(tmp_path / "t.db")
    s = A.AccountStore(db, Config({}), Config({"identity": {"email": "only@example.com"}}))
    assert s.username() == "only@example.com"


def test_an_unanswered_address_is_never_used_as_a_username(tmp_path, vault):
    """ASK is the sentinel for "you have not told me yet". Registering an
    account as "ASK" would be unrecoverable without support."""
    db = Database(tmp_path / "t.db")
    s = A.AccountStore(db, Config({}), Config({
        "identity": {"email": "real@example.com", "email_personal": "ASK"}}))
    assert s.username() == "real@example.com"


@pytest.mark.parametrize("url,expected", [
    ("https://www.acme.com/x", "acme.com"),
    ("https://acme.com/x", "acme.com"),
    ("https://ACME.com/x", "acme.com"),
    ("https://jobs.acme.com/x", "jobs.acme.com"),
])
def test_domain_extraction(url, expected):
    assert A.domain_of(url) == expected


def test_marking_verified_sticks(store):
    store.create("https://acme.com/apply")
    assert store.all()[0]["verified"] == 0
    store.mark_verified("https://acme.com/apply")
    assert store.all()[0]["verified"] == 1
