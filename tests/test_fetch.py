"""Catalogue download: verification, extraction and the CLI wiring.

No network. The archive is built locally and ``_download`` is patched to copy it,
so what is under test is the part that can silently do the wrong thing — a
mismatched digest accepted, a nested archive unpacked into an unreadable layout,
a hostile member name escaping ``dest``.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from astroskysim.cli import main, parse_args
from astroskysim.sky import fetch as fetchmod
from astroskysim.sky.fetch import CatalogRelease, fetch_catalog, resolve_release, sha256_of

# One real cell is enough: StarCatalog globs for <prefix>_*.290 rather than
# probing a specific cell, so availability does not need all 290.
CELL = "g14_0101.290"


def _fake_cell(size: int = 256) -> bytes:
    """A .290-shaped blob. Contents are irrelevant here - nothing decodes it."""
    return bytes(range(256)) * (size // 256)


def _make_archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """A plausible upstream archive: cells nested under a directory, plus attribution."""
    return _make_archive(
        tmp_path / "upstream.zip",
        {
            "g14/": b"",
            f"g14/{CELL}": _fake_cell(),
            "g14/g14_0201.290": _fake_cell(),
            "g14/acknowledgement of databases.txt": b"We acknowledge the usage of the Gaia database",
            "g14/readme.html": b"<html>ignored</html>",
        },
    )


@pytest.fixture
def served(monkeypatch, archive: Path):
    """Patch the transport, keeping verification and extraction real."""
    calls: list[str] = []

    def _copy(url: str, dest: Path, timeout_s: float) -> None:
        calls.append(url)
        shutil.copyfile(archive, dest)

    monkeypatch.setattr(fetchmod, "_download", _copy)
    return calls


def _release(**kw) -> CatalogRelease:
    base = {"name": "g14", "prefix": "g14", "url": "https://example.invalid/g14.zip"}
    return CatalogRelease(**{**base, **kw})


def test_fetch_extracts_cells_flat_and_keeps_the_attribution(served, tmp_path: Path):
    dest = tmp_path / "catalog"
    fetch_catalog(_release(), dest)

    # Flattened out of the g14/ prefix - StarCatalog globs one directory, not a tree.
    assert (dest / CELL).is_file()
    assert (dest / "g14_0201.290").is_file()
    assert not (dest / "g14").exists()
    # The attribution rides along; it is what makes redistribution legitimate.
    assert (dest / "acknowledgement of databases.txt").is_file()
    # Unrelated members stay out.
    assert not (dest / "readme.html").exists()


def test_a_correct_checksum_is_accepted_and_a_wrong_one_refuses(served, archive: Path, tmp_path: Path):
    good = sha256_of(archive)
    fetch_catalog(_release(sha256=good), tmp_path / "ok")
    assert (tmp_path / "ok" / CELL).is_file()

    bad = "0" * 64
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        fetch_catalog(_release(sha256=bad), tmp_path / "bad")
    # Nothing was unpacked from an archive we could not vouch for.
    assert not list((tmp_path / "bad").glob("*.290"))


def test_an_unpinned_release_warns_and_prints_the_digest(served, archive: Path, tmp_path: Path, caplog):
    with caplog.at_level("WARNING"):
        fetch_catalog(_release(sha256=None), tmp_path / "catalog")
    # The digest has to appear, or there is no way to turn this into a pinned entry.
    assert sha256_of(archive) in caplog.text
    assert "no checksum pinned" in caplog.text


def test_a_second_fetch_is_a_no_op_unless_forced(served, tmp_path: Path):
    dest = tmp_path / "catalog"
    fetch_catalog(_release(), dest)
    assert len(served) == 1

    fetch_catalog(_release(), dest)
    assert len(served) == 1, "already-populated directory should not re-download"

    fetch_catalog(_release(), dest, force=True)
    assert len(served) == 2


def test_a_traversing_member_name_cannot_escape_the_destination(monkeypatch, tmp_path: Path):
    """A zip member name is attacker-controlled data, not a path to trust."""
    evil = _make_archive(
        tmp_path / "evil.zip",
        {f"../../{CELL}": _fake_cell(), r"win\subdir\g14_0201.290": _fake_cell()},
    )
    monkeypatch.setattr(fetchmod, "_download", lambda url, dest, timeout_s: shutil.copyfile(evil, dest))

    dest = tmp_path / "deep" / "catalog"
    fetch_catalog(_release(), dest)

    assert (dest / CELL).is_file()
    # Backslash separators from a Windows-built zip are flattened too, not taken
    # as one long filename.
    assert (dest / "g14_0201.290").is_file()
    assert not (tmp_path / CELL).exists()
    assert not (tmp_path / "deep" / CELL).exists()


def test_an_archive_without_cells_is_an_error_naming_what_it_held(monkeypatch, tmp_path: Path):
    empty = _make_archive(tmp_path / "wrong.zip", {"notes.md": b"no cells here"})
    monkeypatch.setattr(fetchmod, "_download", lambda url, dest, timeout_s: shutil.copyfile(empty, dest))

    with pytest.raises(RuntimeError, match="no .290 files inside"):
        fetch_catalog(_release(), tmp_path / "catalog")


def test_an_archive_holding_another_database_is_used_but_flagged(monkeypatch, tmp_path: Path, caplog):
    """g17 cells while g14 was asked for.

    StarCatalog falls back to any database present, so this works rather than
    failing - but the registry entry is then wrong about its own contents, and a
    config asking for g14 quietly gets g17. Say so.
    """
    other = _make_archive(tmp_path / "g17.zip", {"g17_0101.290": _fake_cell()})
    monkeypatch.setattr(fetchmod, "_download", lambda url, dest, timeout_s: shutil.copyfile(other, dest))

    dest = tmp_path / "catalog"
    with caplog.at_level("WARNING"):
        fetch_catalog(_release(), dest)
    assert (dest / "g17_0101.290").is_file()
    assert "expected 'g14' but the archive supplied 'g17'" in caplog.text


def test_another_database_present_does_not_count_as_this_one(monkeypatch, tmp_path: Path):
    """The skip check must not go through StarCatalog's g14->g16->g17 fallback.

    Otherwise a directory holding g17 answers yes to "do you have g14", and
    fetch-catalog reports success without ever downloading g14.
    """
    dest = tmp_path / "catalog"
    dest.mkdir()
    (dest / "g17_0101.290").write_bytes(_fake_cell())

    served: list[str] = []
    archive = _make_archive(tmp_path / "g14.zip", {CELL: _fake_cell()})

    def _copy(url, dest_path, timeout_s):
        served.append(url)
        shutil.copyfile(archive, dest_path)

    monkeypatch.setattr(fetchmod, "_download", _copy)
    fetch_catalog(_release(), dest)

    assert served, "g17 already present must not suppress the g14 download"
    assert (dest / CELL).is_file()


def test_resolve_release_applies_overrides_and_rejects_unknown_names():
    before = resolve_release("g14")
    pinned = resolve_release("g14", url="https://mirror.invalid/x.zip", sha256="AB" * 32)
    assert pinned.url == "https://mirror.invalid/x.zip"
    assert pinned.sha256 == "ab" * 32, "digests are compared lowercase"
    # dataclasses.replace returns a copy - overriding must not edit the table.
    assert resolve_release("g14") == before

    with pytest.raises(KeyError, match="unknown catalogue"):
        resolve_release("g99")


def test_a_bare_invocation_still_means_serve():
    """The subcommand is optional - the pre-existing command line must not break."""
    args = parse_args(["-c", "examples/sim.toml", "-vv"])
    assert args.command is None
    assert args.verbose == 2


def test_verbosity_survives_on_either_side_of_the_subcommand():
    assert parse_args(["-v", "fetch-catalog"]).verbose == 1
    assert parse_args(["fetch-catalog", "-vv"]).verbose == 2
    assert parse_args(["fetch-catalog"]).verbose == 0


def test_cli_fetch_targets_the_configured_catalog_dir(monkeypatch, tmp_path: Path):
    """-c should place the download where the run will look for it."""
    seen: dict[str, object] = {}

    def _spy(release, dest, *, force=False, timeout_s=300.0):
        seen["dest"] = Path(dest)
        return dest

    monkeypatch.setattr("astroskysim.cli.fetch_catalog", _spy)
    cfg = tmp_path / "sim.toml"
    cfg.write_text(f'[source.artificial]\ncatalog_dir = "{tmp_path / "stars"}"\n')

    assert main(["-c", str(cfg), "fetch-catalog"]) == 0
    assert seen["dest"] == tmp_path / "stars"

    # An explicit --dest wins over the config.
    assert main(["-c", str(cfg), "fetch-catalog", "-d", str(tmp_path / "elsewhere")]) == 0
    assert seen["dest"] == tmp_path / "elsewhere"


def test_cli_reports_a_failed_download_as_a_nonzero_exit(monkeypatch, tmp_path: Path):
    def _boom(url, dest, timeout_s):
        raise RuntimeError("HTTP 404 Not Found - has the release asset been uploaded yet?")

    monkeypatch.setattr(fetchmod, "_download", _boom)
    assert main(["fetch-catalog", "-d", str(tmp_path / "catalog")]) == 1
