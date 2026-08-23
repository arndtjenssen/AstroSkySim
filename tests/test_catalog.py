"""Catalogue tests.

The ``.290`` decoder is exercised two ways. The round-trip tests write their own
fixture, so they run with no HNSKY data files present; encoding there is the
inverse of the documented layout, so a bug shows up as a round-trip failure.
That alone cannot catch a *shared* misreading of the format, so the tests under
"real HNSKY data" below decode the real ``catalog/`` set and check it against
the sky: every star must land in the cell its own filename claims, and the
brightest stars must sit at their published position propagated to the epoch the
file header states. Those skip when the catalogue is absent.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from astroskysim.sky.catalog import (
    _DEC_SCALE,
    _RA_SCALE,
    HEADER_BYTES,
    RA_SENTINEL,
    RING_CELLS,
    RING_TOP,
    StarCatalog,
    SyntheticCatalog,
    area_filename,
    area_number,
    areas_covering,
    build_catalog,
    read_area,
    ring_of,
)

# Declination ring boundaries of the 290-cell scheme, as documented for the
# .290 format. Pinned so a refactor of ring_of cannot drift the geometry.
RING_BOUNDARIES_DEG = [
    -85.23224404,
    -75.66348756,
    -65.99286637,
    -56.14497387,
    -46.03163067,
    -35.54307745,
    -24.53348115,
    -12.79440589,
    0.0,
]


def test_ring_geometry_matches_documented_table():
    assert len(RING_CELLS) == 18
    assert sum(RING_CELLS) == 290
    got = np.rad2deg(RING_TOP[:9])
    assert got == pytest.approx(RING_BOUNDARIES_DEG, abs=1e-6)
    # Northern half mirrors the southern.
    assert np.rad2deg(RING_TOP[16]) == pytest.approx(85.23224404, abs=1e-6)
    assert np.rad2deg(RING_TOP[17]) == pytest.approx(90.0)


@pytest.mark.parametrize(
    ("dec_deg", "ring"),
    [(-90, 1), (-80, 2), (-0.001, 9), (0.0, 9), (1.0, 10), (89.0, 18), (90.0, 18)],
)
def test_ring_of(dec_deg, ring):
    assert ring_of(np.deg2rad(dec_deg)) == ring


@pytest.mark.parametrize(
    ("area", "name"),
    [
        (1, "g14_0101.290"),
        (2, "g14_0201.290"),
        (5, "g14_0204.290"),
        (6, "g14_0301.290"),
        (13, "g14_0308.290"),
        (290, "g14_1801.290"),
    ],
)
def test_area_filename_matches_the_published_table(area, name):
    assert area_filename(area, "g14") == name


def test_area_filename_rejects_out_of_range():
    with pytest.raises(ValueError, match="out of range"):
        area_filename(291, "g14")


def test_poles_are_single_areas():
    assert area_number(0.0, np.deg2rad(-90)) == 1
    assert area_number(3.0, np.deg2rad(90)) == 290


def test_area_numbers_are_unique_and_complete():
    """Sweeping the sky must produce exactly the 290 areas, no gaps or repeats."""
    seen = set()
    for dec in np.linspace(-89.9, 89.9, 400):
        for ra in np.linspace(0, 2 * np.pi, 300, endpoint=False):
            seen.add(area_number(ra, np.deg2rad(dec)))
    assert seen == set(range(1, 291))


def test_northern_four_cell_ring_offset():
    """The 4x-RA northern ring starts at cell offset 285."""
    areas = {area_number(ra, np.deg2rad(80.0)) for ra in np.linspace(0, 2 * np.pi, 50, False)}
    assert areas == {286, 287, 288, 289}


def test_areas_covering_small_field_is_local():
    areas = areas_covering(np.deg2rad(83.6), np.deg2rad(22.0), np.deg2rad(0.5))
    assert 1 <= len(areas) <= 4
    assert area_number(np.deg2rad(83.6), np.deg2rad(22.0)) in areas


def test_areas_covering_whole_sky_near_pole():
    areas = areas_covering(0.0, np.deg2rad(89.0), np.deg2rad(30.0))
    assert 290 in areas


# --------------------------------------------------------------------------
# .290 round trip
# --------------------------------------------------------------------------
def _encode_290(stars, record_size=5) -> bytes:
    """Inverse of ``read_area``: build a cell file from (ra_deg, dec_deg, mag)."""
    out = bytearray(b" " * HEADER_BYTES)
    out[109] = record_size

    def emit(ra_raw, dec7, dec8, extra=None):
        rec = bytes(
            [ra_raw & 0xFF, (ra_raw >> 8) & 0xFF, (ra_raw >> 16) & 0xFF, dec7 & 0xFF, dec8 & 0xFF]
        )
        out.extend(rec)
        if record_size == 6:
            out.append((extra or 0) & 0xFF)

    current = None
    for ra_deg, dec_deg, mag in stars:
        dec_raw = int(round(np.deg2rad(dec_deg) / _DEC_SCALE))
        dec9 = dec_raw >> 16  # arithmetic shift keeps the sign
        key = (round(mag * 10), dec9)
        if key != current:
            # Header record: sentinel RA, dec7 carries dec9+128, dec8 carries mag*10+16
            emit(0xFFFFFF, dec9 + 128, round(mag * 10) + 16)
            current = key
        ra_raw = int(round(np.deg2rad(ra_deg % 360) / _RA_SCALE))
        emit(ra_raw, dec_raw & 0xFF, (dec_raw >> 8) & 0xFF)
    return bytes(out)


@pytest.mark.parametrize("record_size", [5, 6])
def test_290_round_trip(tmp_path, record_size):
    stars = [
        (83.6000, 22.0000, 6.5),
        (83.7000, 22.1000, 6.5),
        (84.0000, -22.5000, 9.1),
        (0.0500, 0.0000, -1.4),
        (359.9000, 89.5000, 12.0),
    ]
    p = tmp_path / "g14_0101.290"
    p.write_bytes(_encode_290(stars, record_size))

    field = read_area(p)
    assert len(field) == len(stars)
    order = np.argsort(field.ra)
    want = sorted(stars, key=lambda s: s[0])

    # RA resolution is 0.077", DEC 0.039"; allow a hair over one quantum.
    assert field.ra[order] == pytest.approx([w[0] for w in want], abs=1e-4)
    assert field.dec[order] == pytest.approx([w[1] for w in want], abs=1e-4)
    assert field.mag[order] == pytest.approx([w[2] for w in want], abs=0.05)


def test_290_rejects_unsupported_record_size(tmp_path):
    body = bytearray(b" " * HEADER_BYTES)
    body[109] = 7
    p = tmp_path / "g14_0101.290"
    p.write_bytes(bytes(body) + b"\0" * 70)
    with pytest.raises(ValueError, match="record size 7 is not supported"):
        read_area(p)


def test_290_rejects_truncated_header(tmp_path):
    p = tmp_path / "g14_0101.290"
    p.write_bytes(b"short")
    with pytest.raises(ValueError, match="header"):
        read_area(p)


def test_290_empty_body_is_empty_field(tmp_path):
    body = bytearray(b" " * HEADER_BYTES)
    body[109] = 5
    p = tmp_path / "g14_0101.290"
    p.write_bytes(bytes(body))
    assert len(read_area(p)) == 0


def test_records_before_any_header_are_skipped(tmp_path):
    """Without a preceding header record the magnitude and dec9 are unknown."""
    body = bytearray(b" " * HEADER_BYTES)
    body[109] = 5
    body.extend(bytes([0x10, 0x00, 0x00, 0x00, 0x00]))  # orphan record
    p = tmp_path / "g14_0101.290"
    p.write_bytes(bytes(body))
    assert len(read_area(p)) == 0


# --------------------------------------------------------------------------
# catalogue selection
# --------------------------------------------------------------------------
def test_star_catalog_queries_only_relevant_cells(tmp_path):
    stars = [(83.6, 22.0, 7.0), (83.65, 22.02, 8.0)]
    area = area_number(np.deg2rad(83.6), np.deg2rad(22.0))
    (tmp_path / area_filename(area, "g14")).write_bytes(_encode_290(stars))

    cat = StarCatalog(tmp_path, "g14")
    assert cat.available
    field = cat.query(83.6, 22.0, 0.3)
    assert len(field) == 2

    # A pointing far away hits different (absent) cells and yields nothing.
    assert len(cat.query(200.0, -40.0, 0.3)) == 0


def test_prefix_falls_back_to_an_available_database(tmp_path, caplog):
    (tmp_path / "g16_0101.290").write_bytes(_encode_290([]))
    cat = StarCatalog(tmp_path, "g14")
    assert cat.prefix == "g16"


def test_build_catalog_falls_back_to_synthetic(tmp_path):
    cat = build_catalog(tmp_path, "g14", allow_synthetic=True)
    assert isinstance(cat, SyntheticCatalog)
    assert len(cat.query(83.6, 22.0, 0.5)) > 0


def test_build_catalog_can_refuse_to_fall_back(tmp_path):
    with pytest.raises(FileNotFoundError, match="no .290 star database"):
        build_catalog(tmp_path, "g14", allow_synthetic=False)


def test_synthetic_catalog_is_deterministic():
    a = SyntheticCatalog(seed=7).query(10.0, 20.0, 0.4)
    b = SyntheticCatalog(seed=7).query(10.0, 20.0, 0.4)
    assert np.array_equal(a.ra, b.ra)
    assert np.array_equal(a.mag, b.mag)


def test_synthetic_stars_lie_inside_the_requested_radius():
    f = SyntheticCatalog(seed=3).query(120.0, -15.0, 0.25)
    sep = np.hypot((f.ra - 120.0) * np.cos(np.deg2rad(-15.0)), f.dec + 15.0)
    assert sep.max() <= 0.26


def test_brighter_than_filters():
    f = SyntheticCatalog(seed=1, limiting_mag=15.0).query(0.0, 0.0, 0.5)
    assert f.brighter_than(12.0).mag.max() <= 12.0


# --------------------------------------------------------------------------
# real HNSKY data
#
# The round-trip tests above share their encoder with the decoder, so they
# cannot catch a misreading of the format that both sides make. These decode
# the real g14 set in ``catalog/`` and check it against the sky instead.
# --------------------------------------------------------------------------
CATALOG_DIR = Path(__file__).resolve().parents[1] / "catalog"
HAS_G14 = (CATALOG_DIR / "g14_0101.290").is_file()
needs_g14 = pytest.mark.skipif(not HAS_G14, reason="no real .290 database in catalog/")

#: Epoch the g14 header states. Every astrometric test below propagates to it,
#: so a different vintage of the database invalidates them rather than silently
#: shifting the residuals - hence the explicit assertion in the first test.
G14_EPOCH = 2025.0

_RING_STARTS = np.concatenate(([0], np.cumsum(RING_CELLS)[:-1]))


def _area_numbers(ra_deg, dec_deg):
    """Vectorised ``area_number`` over degree arrays."""
    ring = np.minimum(
        np.searchsorted(RING_TOP, np.deg2rad(dec_deg), side="left") + 1, len(RING_CELLS)
    )
    n = np.asarray(RING_CELLS)[ring - 1]
    cell = np.floor((np.deg2rad(ra_deg) % (2 * np.pi)) * n / (2 * np.pi)).astype(np.int64) % n
    return _RING_STARTS[ring - 1] + 1 + cell


def _arcsec(ra1, dec1, ra2, dec2):
    dra = np.deg2rad(ra1 - ra2) * np.cos(np.deg2rad(dec1))
    return np.hypot(dra, np.deg2rad(dec1 - dec2)) * 206264.806


@pytest.fixture(scope="module")
def g14_cells():
    """Every cell of the real database, decoded once. ~0.3 s for 11.3M stars."""
    if not HAS_G14:
        pytest.skip("no real .290 database in catalog/")
    return {a: read_area(CATALOG_DIR / area_filename(a, "g14")) for a in range(1, 291)}


@needs_g14
def test_the_real_database_is_the_vintage_the_astrometry_tests_assume():
    header = (CATALOG_DIR / "g14_0101.290").read_bytes()[:HEADER_BYTES]
    text = header.decode("latin-1")
    assert "GAIA eDR3" in text
    assert f"Epoch={G14_EPOCH:.0f}" in text
    assert "Magnitude is BP" in text
    assert header[109] == 5, "record size byte"


@needs_g14
def test_every_real_cell_decodes_and_accounts_for_all_its_records(g14_cells):
    for area, field in g14_cells.items():
        path = CATALOG_DIR / area_filename(area, "g14")
        raw = np.frombuffer(path.read_bytes(), dtype=np.uint8)[HEADER_BYTES:]
        records = raw.reshape(-1, 5).astype(np.int64)
        ra_raw = records[:, 0] | (records[:, 1] << 8) | (records[:, 2] << 16)
        headers = np.flatnonzero(ra_raw == RA_SENTINEL)

        # Nothing silently dropped: every record is either a star or a header.
        assert headers.size + len(field) == records.shape[0], path.name
        # The first record must be a magnitude header, or the stars before it
        # would have no magnitude to inherit and would be discarded.
        assert headers.size and headers[0] == 0, path.name
        # Cells are sorted bright to faint, which only holds if the running
        # magnitude carried by the header records is decoded correctly.
        assert np.all(np.diff(field.mag) >= -1e-9), path.name

    total = sum(len(f) for f in g14_cells.values())
    # 11_290_236 in the Aug 2021 set; ~274 stars/sq deg to BP 14.
    assert 9e6 < total < 14e6
    mags = np.concatenate([f.mag for f in g14_cells.values()])
    assert mags.min() == pytest.approx(-1.5, abs=0.05), "Sirius, from the Tycho2 supplement"
    assert mags.max() == pytest.approx(14.0, abs=0.05), "the BP 14 limit the header states"


@needs_g14
def test_real_stars_land_in_the_cell_their_filename_claims(g14_cells):
    """The strongest single check on the decode.

    An error in the RA or Dec scale, the two's-complement Dec sign, the running
    ``dec9`` state or the ring indexing all move stars out of their own cell,
    and the cell assignment was made by whoever built the database rather than
    by us. Ties are expected: a star sitting within one storage quantum of a
    boundary can round either side of it.
    """
    quantum_arcsec = 0.077  # the RA resolution, the coarser of the two
    total = strays = 0
    for area, field in g14_cells.items():
        total += len(field)
        wrong = np.flatnonzero(_area_numbers(field.ra, field.dec) != area)
        strays += wrong.size
        for i in wrong:
            ra, dec = field.ra[i], field.dec[i]
            to_ring = np.min(np.abs(dec - np.rad2deg(RING_TOP))) * 3600
            n = RING_CELLS[min(ring_of(np.deg2rad(dec)), len(RING_CELLS)) - 1]
            frac = (ra % 360.0) * n / 360.0
            to_edge = min(frac % 1.0, 1.0 - frac % 1.0) * (360.0 / n) * 3600
            assert min(to_ring, to_edge) < quantum_arcsec, (
                f"{area_filename(area, 'g14')}: star at {ra:.6f} {dec:+.6f} is in the wrong "
                f"cell and is not on a boundary ({to_ring:.4f}\" from a ring, "
                f"{to_edge:.4f}\" from a cell edge)"
            )
    assert strays / total < 1e-5, f"{strays} of {total} stars in the wrong cell"


@needs_g14
@pytest.mark.parametrize(
    ("ra", "dec", "radius"),
    [
        (300.0, 40.0, 0.5),
        (0.02, 0.0, 0.5),  # RA wrap
        (359.98, -0.01, 0.5),  # RA wrap, other side of the equator ring boundary
        (45.0, 89.8, 0.5),  # north polar cell
        (200.0, -85.3, 0.5),  # just inside the southern 4-cell ring
        (83.82, -5.39, 2.0),  # a field spanning several cells
    ],
)
def test_query_returns_exactly_what_a_brute_force_scan_finds(g14_cells, ra, dec, radius):
    """``areas_covering`` must not miss a cell that clips the field.

    Compared against every one of the 11.3M stars, so a dropped cell shows up as
    a count difference rather than as stars quietly missing from one edge.
    """
    all_ra = np.concatenate([f.ra for f in g14_cells.values()])
    all_dec = np.concatenate([f.dec for f in g14_cells.values()])
    r0, d0 = np.deg2rad(ra), np.deg2rad(dec)
    r, d = np.deg2rad(all_ra), np.deg2rad(all_dec)
    cos_sep = np.sin(d0) * np.sin(d) + np.cos(d0) * np.cos(d) * np.cos(r - r0)
    expected = int((cos_sep >= np.cos(np.deg2rad(radius))).sum())

    assert len(StarCatalog(CATALOG_DIR, "g14").query(ra, dec, radius)) == expected


#: Gaia eDR3 (VizieR I/350) sources within 216" of 300.0 +40.0 with BP < 14:
#: ICRS position and proper motion at Ep=2016.0, and BP magnitude. This is the
#: parent catalogue of g14, fetched independently of anything in this repo.
GAIA_EDR3_AT_300_40 = [
    # ra_deg, dec_deg, pmRA*cosDec, pmDec (mas/yr), BPmag
    (300.00803033666, +39.94348740134, -4.766, -7.660, 12.084),
    (299.97795260908, +40.03561964794, +5.301, -1.342, 12.625),
    (299.97068384280, +39.95289056416, -3.537, -4.318, 12.655),
    (300.01715731964, +40.01969572901, -2.448, -5.464, 13.164),
    (299.99809458968, +40.02128665384, +1.672, -1.478, 13.328),
    (299.94709877220, +39.98612313648, +1.234, -0.058, 13.523),
    (299.98638814758, +40.04970409392, +1.186, -3.802, 13.585),
    (300.02116118506, +40.04296043274, -2.559, -3.703, 13.834),
    (299.99657315271, +40.05428949703, -2.649, -7.742, 13.950),
]


@needs_g14
def test_a_real_field_reproduces_gaia_edr3():
    """Star for star against the parent catalogue, positions and magnitudes.

    Nothing here comes from the ``.290`` files, so this is what makes the decode
    astrometrically correct rather than merely self-consistent. A frame error
    would show as a systematic offset - reading the equinox as of date rather
    than J2000 would displace everything by ~20 arcminutes.
    """
    ref = np.asarray(GAIA_EDR3_AT_300_40)
    dt = G14_EPOCH - 2016.0
    dec = ref[:, 1] + ref[:, 3] * dt / 3.6e6
    ra = ref[:, 0] + (ref[:, 2] * dt / 3.6e6) / np.cos(np.deg2rad(ref[:, 1]))

    field = StarCatalog(CATALOG_DIR, "g14").query(300.0, 40.0, 216 / 3600)
    assert len(field) == len(ref)

    sep = _arcsec(ra[:, None], dec[:, None], field.ra[None, :], field.dec[None, :])
    nearest = sep.argmin(1)
    assert len(set(nearest.tolist())) == len(ref), "matches must be one to one"
    # One RA quantum is 0.077"; the measured residual is ~0.02" rms.
    assert sep[np.arange(len(ref)), nearest].max() < 0.15

    # Magnitude is BP binned to 0.1, so the error is bounded by half a bin. BP
    # 13.950 lands exactly on a bin edge and rounds to 14.0, so allow the edge.
    assert field.mag[nearest] == pytest.approx(ref[:, 4], abs=0.0501)


#: Bright stars, too bright for Gaia and present in g14 via its 82 Tycho2
#: additions. ICRS J2000 position and Hipparcos proper motion, mas/yr. Between
#: them they cover 25 years of motion from 0.03" (Rigel) to 57" (Arcturus).
BRIGHT_STARS_J2000 = [
    ("Sirius", 101.28715533, -16.71611586, -546.01, -1223.07, -1.5),
    ("Arcturus", 213.91530000, +19.18241000, -1093.39, -1999.40, 0.0),
    ("Vega", 279.23473479, +38.78368896, +200.94, +286.23, 0.0),
    ("Rigel", 78.63446707, -8.20163837, +1.31, +0.50, 0.3),
    ("Deneb", 310.35797975, +45.28033881, +2.01, +1.85, 1.4),
    ("Procyon", 114.82549791, +5.22498756, -714.59, -1036.80, 0.4),
    ("Capella", 79.17232794, +45.99799147, +75.52, -427.11, 0.1),
    ("Altair", 297.69582730, +8.86832120, +536.23, +385.29, 1.0),
    ("Betelgeuse", 88.79293899, +7.40706400, +27.33, +10.86, 0.9),
]


@needs_g14
@pytest.mark.parametrize(
    ("name", "ra2000", "dec2000", "pm_ra", "pm_dec", "mag"), BRIGHT_STARS_J2000
)
def test_bright_stars_sit_where_proper_motion_puts_them(name, ra2000, dec2000, pm_ra, pm_dec, mag):
    """Also the only coverage of the Tycho2 bright-star supplement.

    Gaia saturates above about magnitude 3, so nothing brighter reaches the
    database through the Gaia path at all.
    """
    dt = G14_EPOCH - 2000.0
    dec = dec2000 + pm_dec * dt / 3.6e6
    ra = ra2000 + (pm_ra * dt / 3.6e6) / np.cos(np.deg2rad(dec2000))

    field = StarCatalog(CATALOG_DIR, "g14").query(ra, dec, 0.02)
    assert len(field), f"{name} missing from the database"
    sep = _arcsec(ra, dec, field.ra, field.dec)
    i = int(sep.argmin())
    assert sep[i] < 0.5, f"{name} is {sep[i]:.3f}\" from its epoch {G14_EPOCH:.0f} position"
    assert field.mag[i] == pytest.approx(mag, abs=0.05)
