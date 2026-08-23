"""Fetching a ``.290`` star database into the catalogue directory.

The g14 set this project is developed against (GAIA eDR3 to BP 14.0, epoch 2025,
290 cells, 11,290,236 stars, ~56 MB) is **no longer distributed upstream**.
SourceForge now carries g17/g18/v16/v17/u16/tuc for HNSKY and the d/v/g-tier
databases for ASTAP, all of them 100 MB to 1.3 GB. There is therefore no "just
point at the original" option for g14, and a pinned mirror is the only thing that
keeps ``catalog = "g14"`` reproducible.

Two consequences shape this module:

* **No checksum is baked in until someone publishes one.** A release with
  ``sha256=None`` still downloads, but says out loud that it is unverified and
  prints the digest it got — which is exactly the step that turns a fresh upload
  into a pinned entry. A *fabricated* checksum would be strictly worse than none:
  every download would fail for a reason nobody could diagnose from the message.
* **The archive layout is not trusted.** Only ``*.290`` and the acknowledgement
  text are extracted, by basename, into a flat directory. HNSKY's own archives
  nest the cells under a program directory, and a member name inside a zip is
  attacker-controlled data — a ``../`` in one writes outside ``dest``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .catalog import StarCatalog

log = logging.getLogger("astroskysim.fetch")

USER_AGENT = "astroskysim (+https://github.com/arndtjenssen/AstroSkySim)"

#: Read size for hashing and streaming. Large enough that a 56 MB archive is not
#: 14000 syscalls, small enough to stay off the large-object heap.
_CHUNK = 1 << 20


@dataclass(frozen=True, slots=True)
class CatalogRelease:
    """One downloadable star database."""

    #: Registry key, and what the user types after ``fetch-catalog``.
    name: str
    #: Cell file prefix the archive is expected to contain (``g14`` -> g14_0101.290).
    #: Used both to skip an already-populated directory and to verify the result.
    prefix: str
    url: str
    #: SHA-256 of the archive. ``None`` means nothing has been pinned yet: the
    #: download proceeds, unverified and loudly.
    sha256: str | None = None
    approx_mb: int | None = None
    note: str = ""


#: Where a mirrored catalogue lives. A data asset gets its own tag so it is
#: never confused with a software release — and note that publishing it as a
#: normal release makes it GitHub's "latest", which is why the URL is pinned to
#: the tag rather than going through ``/releases/latest/download/``.
MIRROR_TAG = "catalog-g14-v1"
MIRROR_URL = f"https://github.com/arndtjenssen/AstroSkySim/releases/download/{MIRROR_TAG}"

RELEASES: dict[str, CatalogRelease] = {
    # The digest is of the archive built by the `zip -r -9` line in the README:
    # the 290 g14 cells plus the acknowledgement, flat, no enclosing directory.
    # Zip output is not reproducible across tools, so re-zipping the same files
    # with something else needs this re-pinned - run the command and paste back
    # the digest it prints.
    "g14": CatalogRelease(
        name="g14",
        prefix="g14",
        url=f"{MIRROR_URL}/g14_star_database_mag14.zip",
        sha256="6f146ae89a7c6f77e51fb43789ced65e66a1d33b3e91a3cb9f31b9f34bbc3223",
        approx_mb=56,
        note="GAIA eDR3 to BP 14.0, epoch 2025. Retired upstream; mirrored here.",
    ),
    # An upstream-hosted alternative, so this command works without the mirror.
    # Verified: 290 .290 cells, record size 5, decoded by the existing reader with
    # no changes, 20,663,798 stars, astrometry matching Gaia to 0.02" rms.
    #
    # The digest is upstream's file, not ours, so ASTAP publishing a new g05 breaks
    # it deliberately: everything astrometric here depends on this data, and
    # "the bytes changed under you" is worth a hard failure and a re-pin.
    #
    # NOT a drop-in for g14 - see the README. ASTAP's suffix is *density*
    # (<=500 stars/sq deg), not magnitude, so it is deeper than g14 at high
    # galactic latitude and *shallower* in the plane, where it discards stars
    # g14 has.
    "g05": CatalogRelease(
        name="g05",
        prefix="g05",
        url="https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.zip/download",
        sha256="2e00b5b327b967570415fd3a0b99067e1d687c819fb049e42c374e56b6fa3f0a",
        approx_mb=102,
        note="GAIA DR3, density <=500 stars/sq deg, epoch 2025. Flattens galactic density contrast.",
    ),
}

DEFAULT_RELEASE = "g14"


def _has_cells(directory: Path, prefix: str) -> bool:
    """Whether *this exact* database is present.

    Deliberately not ``StarCatalog.available``: that resolves through the
    g14 -> g16 -> g17 -> g18 -> u16 fallback, so a directory holding g17 would
    answer yes to "do you have g14" and the download would be skipped forever.
    """
    return next(directory.glob(f"{prefix}_*.290"), None) is not None


def sha256_of(path: Path) -> str:
    """Streaming digest — the archives are tens to hundreds of MB."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _wanted(member: str) -> bool:
    """The cells, plus the attribution that makes redistributing them legitimate."""
    low = member.lower()
    if low.endswith("/"):
        return False
    return low.endswith(".290") or (low.endswith(".txt") and "acknowledge" in low)


def _flat_name(member: str) -> str:
    """Basename of a zip member, treating the name as hostile.

    Zip stores ``/`` by spec but Windows tools have shipped ``\\``, and a member
    called ``../../.ssh/authorized_keys`` is a valid zip. Taking only the final
    component defuses both, and flattening is what we want anyway: HNSKY's
    archives nest the cells one or two directories deep.
    """
    return PurePosixPath(member.replace("\\", "/")).name


def _progress(done: int, total: int) -> None:
    if not sys.stderr.isatty():
        return
    mb = done / 1e6
    if total:
        print(f"\r  {mb:6.1f} / {total / 1e6:.1f} MB  ({done * 100 // total:3d}%)", end="", file=sys.stderr)
    else:
        print(f"\r  {mb:6.1f} MB", end="", file=sys.stderr)


def _download(url: str, dest: Path, timeout_s: float) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            # A 200 carrying HTML is a mirror interstitial or a "no such release"
            # page, not the archive. Left alone it lands as a few KB of markup and
            # surfaces as "File is not a zip file", which points nowhere near the
            # actual problem.
            ctype = resp.headers.get_content_type()
            if ctype in ("text/html", "application/xhtml+xml"):
                raise RuntimeError(
                    f"{url} returned {ctype}, not an archive - the release asset is "
                    "probably missing, or the host served a download interstitial"
                )
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with dest.open("wb") as out:
                while chunk := resp.read(_CHUNK):
                    out.write(chunk)
                    done += len(chunk)
                    _progress(done, total)
    except urllib.error.HTTPError as exc:
        hint = " - has the release asset been uploaded yet?" if exc.code == 404 else ""
        raise RuntimeError(f"{url}: HTTP {exc.code} {exc.reason}{hint}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{url}: {exc.reason}") from exc
    finally:
        if sys.stderr.isatty():
            print(file=sys.stderr)


def _extract(archive: Path, dest: Path) -> int:
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"{archive.name}: not a zip archive ({exc})") from exc
    with zf:
        members = [m for m in zf.infolist() if _wanted(m.filename)]
        if not members:
            raise RuntimeError(
                f"{archive.name}: no .290 files inside "
                f"(saw {', '.join(m.filename for m in zf.infolist()[:5]) or 'nothing'})"
            )
        for m in members:
            name = _flat_name(m.filename)
            if not name:
                continue
            with zf.open(m) as src, (dest / name).open("wb") as out:
                shutil.copyfileobj(src, out, _CHUNK)
    return len(members)


def fetch_catalog(
    release: CatalogRelease,
    dest: Path,
    *,
    force: bool = False,
    timeout_s: float = 300.0,
) -> Path:
    """Download and unpack ``release`` into ``dest``. Idempotent unless ``force``.

    Raises ``RuntimeError`` on a bad download, a checksum mismatch, or an archive
    that unpacks into something ``StarCatalog`` cannot read.
    """
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    if not force and _has_cells(dest, release.prefix):
        log.info("%r already present in %s - nothing to do (--force to re-fetch)", release.prefix, dest)
        return dest

    log.info(
        "fetching %s (~%s MB) from %s",
        release.name,
        release.approx_mb or "?",
        release.url,
    )
    with tempfile.TemporaryDirectory(prefix="astroskysim-catalog-") as tmp:
        archive = Path(tmp) / "catalog.zip"
        _download(release.url, archive, timeout_s)

        digest = sha256_of(archive)
        if release.sha256 is None:
            log.warning(
                "no checksum pinned for %r - downloaded %s unverified. Pin it in "
                "sky/fetch.py: sha256=%r",
                release.name,
                archive.name,
                digest,
            )
        elif digest != release.sha256:
            raise RuntimeError(
                f"checksum mismatch for {release.name}: expected {release.sha256}, got {digest}. "
                "The asset was replaced, or the download was truncated or intercepted."
            )
        else:
            log.info("checksum ok (%s)", digest[:16])

        n = _extract(archive, dest)

    cat = StarCatalog(dest, release.prefix)
    if not cat.available:
        raise RuntimeError(
            f"extracted {n} files into {dest} but no .290 cells are readable there - "
            "the archive did not hold a star database"
        )
    if cat.prefix != release.prefix:
        # Usable, since StarCatalog falls back to any database present, but say so:
        # the archive is not what the registry entry claims it is, and a config
        # asking for `catalog = "<release.prefix>"` will silently get this instead.
        log.warning(
            "expected %r but the archive supplied %r - the registry entry for %s is wrong",
            release.prefix,
            cat.prefix,
            release.name,
        )
    log.info("extracted %d files into %s; star database %r is ready", n, dest, cat.prefix)
    return dest


def resolve_release(name: str, url: str | None = None, sha256: str | None = None) -> CatalogRelease:
    """Registry entry for ``name``, with optional url/checksum overrides.

    The overrides are what make an unpublished mirror usable: point at any URL,
    pin any digest, without editing the table first.
    """
    try:
        release = RELEASES[name]
    except KeyError:
        known = ", ".join(sorted(RELEASES))
        raise KeyError(f"unknown catalogue {name!r} (known: {known})") from None
    changes: dict[str, object] = {}
    if url:
        changes["url"] = url
    if sha256:
        changes["sha256"] = sha256.lower().strip()
    return dataclasses.replace(release, **changes) if changes else release
