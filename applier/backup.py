"""Copying out the parts of this that cannot be rebuilt.

Most of the system is replaceable: the code is in git, the job table can be
rediscovered in an afternoon, artifacts are regenerated from the bank. Three
things are not.

    config/profile.yaml   answers that go onto immigration-relevant forms
    config/bank/          the content bank, which is months of writing
    data/applier.db       what has been applied to, and what was answered

Losing those is not a setback, it is starting again. They live in one
directory and nothing was copying it anywhere.

Deliberately a plain zip rather than anything clever: the whole point of a
backup is that it can be opened in five years by something other than this
program. API keys are never in it -- they live in the OS keychain, which is
not a file, and an archive that contained them would be a plaintext secret
sitting in a Downloads folder.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ROOT

# What goes in, relative to the data root. Ordered most to least precious, so
# a truncated archive still has the irreplaceable part.
INCLUDE = ("config", "data", "private")

# What never does. Artifacts are reproducible and dominate the size; caches
# and journals are meaningless outside their moment.
EXCLUDE_DIRS = {"__pycache__", ".browser-profile", "node_modules", ".git"}
EXCLUDE_SUFFIXES = (".db-wal", ".db-shm", ".pyc", ".tmp", ".log")

MANIFEST = "applier-backup.json"


@dataclass
class BackupResult:
    path: Path
    files: int
    bytes: int

    @property
    def megabytes(self) -> float:
        return self.bytes / 1_048_576


def _wanted(path: Path) -> bool:
    if any(part in EXCLUDE_DIRS for part in path.parts):
        return False
    return not path.name.endswith(EXCLUDE_SUFFIXES)


def create(dest: Path | None = None, *, root: Path | None = None) -> BackupResult:
    """Write a zip of everything that cannot be regenerated."""
    root = root or ROOT
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = Path(dest) if dest else root / "out" / f"applier-backup-{stamp}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)

    files = 0
    total = 0
    # Written to a temporary name and moved into place, so an interrupted
    # backup never replaces a good one with a truncated one.
    staging = dest.with_suffix(".zip.partial")
    with zipfile.ZipFile(staging, "w", zipfile.ZIP_DEFLATED) as zf:
        for top in INCLUDE:
            base = root / top
            if not base.exists():
                continue
            for item in sorted(base.rglob("*")):
                if not item.is_file() or not _wanted(item.relative_to(root)):
                    continue
                zf.write(item, str(item.relative_to(root)).replace("\\", "/"))
                files += 1
                total += item.stat().st_size
        zf.writestr(MANIFEST, json.dumps({
            "created": datetime.now().isoformat(timespec="seconds"),
            "root": str(root),
            "files": files,
            "bytes": total,
            "includes": list(INCLUDE),
            "note": "API keys are NOT in this archive. They live in the OS "
                    "keychain and must be set again with `applier config set-key`.",
        }, indent=2))
    staging.replace(dest)
    return BackupResult(dest, files, total)


def inspect(archive: Path) -> dict:
    """What is in an archive, without unpacking it."""
    with zipfile.ZipFile(archive) as zf:
        try:
            meta = json.loads(zf.read(MANIFEST))
        except KeyError:
            meta = {"note": "no manifest; not written by this program"}
        names = [n for n in zf.namelist() if n != MANIFEST]
    meta["entries"] = len(names)
    meta["has_profile"] = any(n.endswith("config/profile.yaml") for n in names)
    meta["has_bank"] = any("config/bank/" in n for n in names)
    meta["has_database"] = any(n.endswith(".db") for n in names)
    return meta


def restore(archive: Path, *, root: Path | None = None,
            overwrite: bool = False) -> list[str]:
    """Unpack an archive over the data directory. Returns what was written.

    Refuses to overwrite by default. A restore that silently replaces a
    profile you have since corrected is the same accident as not having a
    backup, in the other direction -- and the existing files are moved aside
    rather than deleted even when overwrite is asked for.
    """
    root = root or ROOT
    written: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        members = [n for n in zf.namelist() if n != MANIFEST]

        # Refuse path traversal. A zip is an untrusted container even when it
        # came from here: "../../.ssh/authorized_keys" is a valid entry name.
        # Both sides resolved, or the comparison is meaningless. On Windows a
        # directory can resolve through a junction or an app-container
        # redirection -- the data directory became
        # a path under AppData/Local/Packages/.../LocalCache -- so an
        # unresolved root never prefixes a resolved target and every single
        # entry read as an escape attempt.
        safe_root = Path(root).resolve()
        for name in members:
            target = (safe_root / name).resolve()
            if not target.is_relative_to(safe_root):
                raise ValueError(f"archive entry escapes the data directory: {name}")

        clashes = [n for n in members if (root / n).exists()]
        if clashes and not overwrite:
            raise FileExistsError(
                f"{len(clashes)} file(s) already exist, starting with "
                f"{clashes[0]}. Pass overwrite to replace them; the current "
                f"versions are moved aside, not deleted.")

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for name in members:
            target = safe_root / name
            if target.exists():
                aside = target.with_name(f"{target.name}.replaced-{stamp}")
                shutil.move(str(target), str(aside))
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            written.append(name)
    return written
