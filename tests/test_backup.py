"""Copying out what cannot be rebuilt, and refusing to be a way in.

Most of this system is replaceable: the code is in git, the job table can be
rediscovered in an afternoon, artifacts regenerate from the bank. The profile,
the content bank and the record of what has been applied to are not. They live
in one directory and nothing was copying it anywhere.

A backup is also a container of untrusted data the moment it leaves the
machine, so restore treats it as one.
"""

from __future__ import annotations

import zipfile

import pytest

from applier import backup as B


@pytest.fixture
def home(tmp_path):
    (tmp_path / "config" / "bank").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "applications" / "x").mkdir(parents=True)
    (tmp_path / "config" / "profile.yaml").write_text("identity:\n  full_name: A\n",
                                                      encoding="utf-8")
    (tmp_path / "config" / "bank" / "atoms.yaml").write_text("atoms: []\n", encoding="utf-8")
    (tmp_path / "data" / "applier.db").write_bytes(b"sqlite")
    (tmp_path / "data" / "applier.db-wal").write_bytes(b"journal")
    (tmp_path / "applications" / "x" / "resume.pdf").write_bytes(b"%PDF")
    return tmp_path


def test_it_contains_the_three_things_that_matter(home, tmp_path):
    out = B.create(tmp_path / "b.zip", root=home)
    with zipfile.ZipFile(out.path) as zf:
        names = set(zf.namelist())
    assert "config/profile.yaml" in names
    assert "config/bank/atoms.yaml" in names
    assert "data/applier.db" in names


def test_it_leaves_out_what_regenerates(home, tmp_path):
    out = B.create(tmp_path / "b.zip", root=home)
    with zipfile.ZipFile(out.path) as zf:
        names = set(zf.namelist())
    # Artifacts are reproducible from the bank and dominate the size.
    assert not any(n.startswith("applications/") for n in names)
    # A write-ahead journal is meaningless outside its moment.
    assert not any(n.endswith(("-wal", "-shm")) for n in names)


def test_the_manifest_says_keys_are_not_included(home, tmp_path):
    """A restore on a new machine fails confusingly without this sentence."""
    out = B.create(tmp_path / "b.zip", root=home)
    meta = B.inspect(out.path)
    assert "keychain" in meta["note"]
    assert meta["has_profile"] and meta["has_bank"] and meta["has_database"]


def test_an_interrupted_backup_does_not_replace_a_good_one(home, tmp_path):
    dest = tmp_path / "b.zip"
    B.create(dest, root=home)
    assert dest.exists()
    assert not dest.with_suffix(".zip.partial").exists(), "staging file left behind"


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def test_restore_refuses_to_overwrite_by_default(home, tmp_path):
    out = B.create(tmp_path / "b.zip", root=home)
    with pytest.raises(FileExistsError):
        B.restore(out.path, root=home)


def test_restore_moves_the_old_file_aside_rather_than_deleting_it(home, tmp_path):
    out = B.create(tmp_path / "b.zip", root=home)
    (home / "config" / "profile.yaml").write_text("identity:\n  full_name: CHANGED\n",
                                                  encoding="utf-8")
    B.restore(out.path, root=home, overwrite=True)

    assert "full_name: A" in (home / "config" / "profile.yaml").read_text(encoding="utf-8")
    aside = list((home / "config").glob("profile.yaml.replaced-*"))
    assert aside, "the version being replaced was deleted outright"
    assert "CHANGED" in aside[0].read_text(encoding="utf-8")


def test_restore_into_an_empty_directory_works(home, tmp_path):
    out = B.create(tmp_path / "b.zip", root=home)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    written = B.restore(out.path, root=fresh)
    assert (fresh / "config" / "profile.yaml").exists()
    assert (fresh / "data" / "applier.db").exists()
    assert len(written) >= 3


def test_an_archive_cannot_write_outside_the_data_directory(tmp_path):
    """A zip is an untrusted container even when this program wrote it.

    "../../.ssh/authorized_keys" is a valid entry name, and extracting it is
    the classic way a backup tool becomes a way in.
    """
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escaped.txt", "owned")

    target = tmp_path / "home"
    target.mkdir()
    with pytest.raises(ValueError, match="escapes"):
        B.restore(evil, root=target)
    assert not (tmp_path.parent / "escaped.txt").exists()


@pytest.mark.parametrize("name", [
    "../x", "../../x", "a/../../x", "/abs/x",
])
def test_every_traversal_shape_is_refused(name, tmp_path):
    evil = tmp_path / "e.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr(name, "x")
    target = tmp_path / "home"
    target.mkdir()
    try:
        B.restore(evil, root=target)
    except (ValueError, OSError):
        return
    written = [p for p in target.rglob("*") if p.is_file()]
    for p in written:
        assert str(p.resolve()).startswith(str(target.resolve()))
