"""Star and deep-sky catalogues.

Primary source is the HNSKY ``.290`` binary format, as used by the HNSKY and
ASTAP star databases. The sky is divided into 290 equal-area cells stored as
separate files (``g14_0101.290`` … ), each sorted bright to faint.

Record layout (sizes 5 and 6 only — the two the published databases use):

* 110-byte header; byte 109 holds the record size (a space means 11).
* ``ra7,ra8,ra9,dec7,dec8`` and, for size 6, a signed ``B_R`` (Gaia Bp-Rp x10).
* ``ra_raw == 0xFFFFFF`` marks a **header record** carrying running state for
  the records that follow: ``mag*10 = dec8 - 16`` and ``dec9 = dec7 - 128``.
* ``ra  = ra_raw * 2pi/(256**3 - 1)``
* ``dec = ((dec9<<16) + (dec8<<8) + dec7) * (pi/2)/(128*256**2 - 1)``

Reading is vectorised: the whole cell file is decoded at once and the running
magnitude/dec9 state is forward-filled with an accumulate, rather than looped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("astroskysim.catalog")

HEADER_BYTES = 110
RA_SENTINEL = 0xFFFFFF
_RA_SCALE = 2.0 * np.pi / (256**3 - 1)
_DEC_SCALE = (np.pi / 2.0) / (128 * 256 * 256 - 1)

#: Cells per declination ring, south pole first. Sums to 290.
RING_CELLS = (1, 4, 8, 12, 16, 20, 24, 28, 32, 32, 28, 24, 20, 16, 12, 8, 4, 1)

#: Cumulative equal-area counts used for the ring boundaries: asin(1 - k/289).
_CUM = (1, 9, 25, 49, 81, 121, 169, 225, 289)


def _ring_top_boundaries() -> np.ndarray:
    """Upper declination boundary of each ring, radians, index 0 = ring 1."""
    south = [-np.arcsin(1.0 - k / 289.0) for k in _CUM]  # rings 1..9, [8] == 0
    north = [-s for s in reversed(south[:-1])]  # rings 10..17
    return np.array([*south, *north, np.pi / 2.0])


RING_TOP = _ring_top_boundaries()

#: Area number of the first cell in each ring (1-based areas), index 0 = ring 1.
_RING_OFFSET = np.cumsum((0,) + RING_CELLS[:-1])


@dataclass(slots=True)
class StarField:
    """Stars in a region. Arrays are parallel and in degrees / magnitudes."""

    ra: np.ndarray
    dec: np.ndarray
    mag: np.ndarray
    #: Gaia Bp-Rp colour, NaN when the record size does not carry it.
    bp_rp: np.ndarray

    def __len__(self) -> int:
        return int(self.ra.size)

    @classmethod
    def empty(cls) -> StarField:
        z = np.empty(0, dtype=np.float64)
        return cls(z, z.copy(), z.copy(), z.copy())

    def concat(self, other: StarField) -> StarField:
        return StarField(
            np.concatenate([self.ra, other.ra]),
            np.concatenate([self.dec, other.dec]),
            np.concatenate([self.mag, other.mag]),
            np.concatenate([self.bp_rp, other.bp_rp]),
        )

    def brighter_than(self, limit: float) -> StarField:
        m = self.mag <= limit
        return StarField(self.ra[m], self.dec[m], self.mag[m], self.bp_rp[m])


def ring_of(dec_rad: float) -> int:
    """1-based ring index containing ``dec_rad``."""
    return int(np.searchsorted(RING_TOP, dec_rad, side="left")) + 1


def area_number(ra_rad: float, dec_rad: float) -> int:
    """1-based area (cell) number, matching ``area_and_boundaries``."""
    ring = min(ring_of(dec_rad), len(RING_CELLS))
    n = RING_CELLS[ring - 1]
    cell = int(np.floor((ra_rad % (2 * np.pi)) * n / (2 * np.pi))) % n
    return int(_RING_OFFSET[ring - 1]) + 1 + cell


def area_filename(area: int, prefix: str) -> str:
    """``g14_0101.290`` style name for a 1-based area number."""
    if not 1 <= area <= 290:
        raise ValueError(f"area {area} out of range 1..290")
    ring = int(np.searchsorted(_RING_OFFSET, area - 1, side="right"))
    cell = area - int(_RING_OFFSET[ring - 1])
    return f"{prefix}_{ring:02d}{cell:02d}.290"


def areas_covering(ra_rad: float, dec_rad: float, radius_rad: float) -> list[int]:
    """Every area whose cell could intersect a circle of ``radius_rad``.

    Cells are up to ~10 degrees across, so a normal camera field touches one to
    four of them. We walk the rings the circle spans and, in each, the RA cells
    the circle spans, widening RA by 1/cos(dec) for the declination squeeze.
    """
    dec_lo = max(dec_rad - radius_rad, -np.pi / 2)
    dec_hi = min(dec_rad + radius_rad, np.pi / 2)
    ring_lo, ring_hi = ring_of(dec_lo), ring_of(dec_hi)

    out: set[int] = set()
    for ring in range(ring_lo, min(ring_hi, len(RING_CELLS)) + 1):
        n = RING_CELLS[ring - 1]
        offset = int(_RING_OFFSET[ring - 1])
        if n == 1:
            out.add(offset + 1)
            continue
        # Widen in RA by the cos(dec) squeeze, using the ring edge nearest a pole.
        edge = max(abs(RING_TOP[ring - 1]), abs(RING_TOP[ring - 2] if ring > 1 else 0.0))
        cos_dec = max(np.cos(min(edge, np.deg2rad(89.5))), 1e-3)
        dra = min(radius_rad / cos_dec, np.pi)
        span = 2 * dra
        if span >= 2 * np.pi:
            out.update(offset + 1 + c for c in range(n))
            continue
        start = int(np.floor(((ra_rad - dra) % (2 * np.pi)) * n / (2 * np.pi)))
        steps = int(np.floor(span * n / (2 * np.pi))) + 2
        out.update(offset + 1 + ((start + s) % n) for s in range(steps))
    return sorted(out)


def read_area(path: Path) -> StarField:
    """Decode one ``.290`` cell file."""
    raw = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    if raw.size < HEADER_BYTES:
        raise ValueError(f"{path.name}: shorter than the {HEADER_BYTES}-byte header")

    size_byte = int(raw[109])
    rec = 11 if size_byte == 0x20 else size_byte  # space means the 11-byte default
    if rec not in (5, 6):
        raise ValueError(
            f"{path.name}: record size {rec} is not supported "
            "(only the 5- and 6-byte layouts are implemented)"
        )

    body = raw[HEADER_BYTES:]
    n = body.size // rec
    if n == 0:
        return StarField.empty()
    b = body[: n * rec].reshape(n, rec).astype(np.int64)

    ra_raw = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
    is_header = ra_raw == RA_SENTINEL

    # Forward-fill the running (mag, dec9) state from each header record.
    idx = np.where(is_header, np.arange(n), -1)
    last = np.maximum.accumulate(idx)
    valid = (~is_header) & (last >= 0)
    if not valid.any():
        return StarField.empty()

    src = last[valid]
    mag = (b[src, 4] - 16) / 10.0
    dec9 = b[src, 3] - 128

    dec_raw = (dec9 << 16) | (b[valid, 4] << 8) | b[valid, 3]
    ra = ra_raw[valid] * _RA_SCALE
    dec = dec_raw * _DEC_SCALE

    if rec == 6:
        bp_rp = b[valid, 5].astype(np.float64)
        bp_rp[bp_rp > 127] -= 256  # stored as a signed byte
        bp_rp /= 10.0
    else:
        bp_rp = np.full(ra.size, np.nan)

    return StarField(np.rad2deg(ra), np.rad2deg(dec), mag, bp_rp)


class StarCatalog:
    """Cell-cached reader over a directory of ``.290`` files."""

    #: Fallback order, matching ``select_star_database``.
    #:
    #: The magnitude-limited HNSKY names come first, then ASTAP's current
    #: density-limited tiers, whose suffix is stars per square degree rather than
    #: a magnitude (g05 = 500/sq deg). Preference order only matters when two
    #: databases share a directory, so appending keeps existing setups unchanged
    #: while letting a directory holding *only* an ASTAP database be found at all
    #: - without these, a config asking for "g14" over a g05 directory silently
    #: fell through to the synthetic field.
    #:
    #: Listing a prefix costs nothing if it ships in another format: the probe
    #: globs ``<prefix>_*.290``, so ASTAP's larger ``.1476`` databases simply do
    #: not match rather than being half-read.
    KNOWN = (
        "g14", "g16", "g17", "g18", "u16", "v16", "v17", "tuc",
        "g05", "v05", "d05", "d20", "d50", "d80", "w08",
    )  # fmt: skip

    def __init__(self, directory: Path | None, prefix: str = "g14") -> None:
        self.directory = Path(directory).expanduser() if directory else None
        self.prefix = self._resolve_prefix(prefix)
        self._cache: dict[int, StarField] = {}

    def _resolve_prefix(self, preferred: str) -> str | None:
        """Pick a database prefix that actually has cell files present.

        Probing for ``<prefix>_0101.290`` alone would read a database missing
        that one south-pole cell as absent entirely, so we glob instead. That
        also accepts a partial database - missing cells simply return no stars.
        """
        if self.directory is None or not self.directory.is_dir():
            return None
        for name in (preferred, *self.KNOWN):
            if next(self.directory.glob(f"{name}_*.290"), None) is not None:
                if name != preferred:
                    log.warning("star database %r not found, using %r", preferred, name)
                return name
        return None

    @property
    def available(self) -> bool:
        return self.prefix is not None

    def query(self, ra_deg: float, dec_deg: float, radius_deg: float) -> StarField:
        """Stars within ``radius_deg`` of the given position."""
        if not self.available:
            return StarField.empty()
        ra_rad, dec_rad = np.deg2rad(ra_deg), np.deg2rad(dec_deg)
        field = StarField.empty()
        for area in areas_covering(ra_rad, dec_rad, np.deg2rad(radius_deg)):
            cell = self._cache.get(area)
            if cell is None:
                path = self.directory / area_filename(area, self.prefix)  # type: ignore[arg-type]
                try:
                    cell = read_area(path)
                except (OSError, ValueError) as exc:
                    log.warning("%s: %s", path.name, exc)
                    cell = StarField.empty()
                self._cache[area] = cell
            field = field.concat(cell)
        return _within(field, ra_deg, dec_deg, radius_deg)


def _within(field: StarField, ra_deg: float, dec_deg: float, radius_deg: float) -> StarField:
    if len(field) == 0:
        return field
    ra0, dec0 = np.deg2rad(ra_deg), np.deg2rad(dec_deg)
    ra, dec = np.deg2rad(field.ra), np.deg2rad(field.dec)
    # Haversine-free: the dot product is enough at these separations.
    cosd = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    m = cosd >= np.cos(np.deg2rad(radius_deg))
    return StarField(field.ra[m], field.dec[m], field.mag[m], field.bp_rp[m])


class SyntheticCatalog:
    """Seeded procedural star field, so the simulator runs with no data files.

    Counts follow a rough log N ~ 0.6 m power law, which is close enough to the
    real sky for focus, guiding and framing tests. It is *not* astrometrically
    real, so plate solving against it will fail - use a ``.290`` database for that.
    """

    def __init__(self, seed: int | None = 1234, limiting_mag: float = 16.0) -> None:
        self.seed = seed
        self.limiting_mag = limiting_mag

    available = True

    def query(self, ra_deg: float, dec_deg: float, radius_deg: float) -> StarField:
        # Seed from the pointing so the same field always renders identically.
        key = (
            0 if self.seed is None else self.seed,
            int(round(ra_deg * 3600)),
            int(round(dec_deg * 3600)),
        )
        rng = np.random.default_rng(abs(hash(key)) % (2**32))

        area = np.pi * radius_deg**2
        density = 10 ** (0.6 * min(self.limiting_mag, 20.0) - 5.2)  # per sq deg
        n = max(int(rng.poisson(density * area)), 1)

        # Uniform on the cap around the pointing.
        t = rng.uniform(0, 2 * np.pi, n)
        r = radius_deg * np.sqrt(rng.uniform(0, 1, n))
        dec = dec_deg + r * np.cos(t)
        cos_dec = max(np.cos(np.deg2rad(dec_deg)), 1e-3)
        ra = ra_deg + r * np.sin(t) / cos_dec

        # Faint stars dominate; clip to a sane bright end.
        mag = self.limiting_mag - rng.exponential(1.6, n)
        return StarField(
            ra % 360.0,
            np.clip(dec, -90.0, 90.0),
            np.clip(mag, -1.5, self.limiting_mag),
            np.full(n, np.nan),
        )


def build_catalog(
    directory: Path | None,
    prefix: str = "g14",
    *,
    allow_synthetic: bool = True,
    seed: int | None = 1234,
    limiting_mag: float = 16.0,
) -> StarCatalog | SyntheticCatalog:
    """Real catalogue if the files are there, else the synthetic fallback."""
    cat = StarCatalog(directory, prefix)
    if cat.available:
        log.info("star database %r from %s", cat.prefix, cat.directory)
        return cat
    if not allow_synthetic:
        raise FileNotFoundError(
            f"no .290 star database found in {directory!r} "
            "(expected e.g. g14_0101.290); set allow_synthetic_fallback to run without one"
        )
    log.warning(
        "no .290 star database in %s - using the synthetic field. "
        "Plate solving will not work against it.",
        directory,
    )
    return SyntheticCatalog(seed=seed, limiting_mag=limiting_mag)
