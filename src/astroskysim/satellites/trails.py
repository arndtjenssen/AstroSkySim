"""Satellite trails: SGP4 in, electrons on the sensor grid out.

A satellite is a point source that moves, so a trail is the *same* PSF the stars
get, swept along the path the satellite takes during the exposure. Everything
photometric follows from that and from the optics already in play:

* brightness comes from ``magnitude_to_electrons`` — the one photometric scale
  the stars, the sky and the survey cutout use — so a trail brightens with
  aperture and dims through a narrowband filter exactly as it should;
* the **surface** brightness of a trail is flux divided by how fast it crosses,
  which falls out of depositing ``rate x dt`` at each sampled point rather than
  being modelled. A Starlink at 1.1 deg/s lays down a faint line; a
  geostationary satellite drifting at the sidereal rate lays down a bright short
  one from the same magnitude.

Two rules shape the propagation, and both exist because the naive version is
either wrong or unaffordable:

* **The search is coarse and the draw is fine.** Propagating 12000 objects at
  the sampling a sub-pixel trail needs is millions of SGP4 calls per frame. So
  one coarse pass over every object finds the few whose line of sight comes
  within a cone of the field, and only those are propagated finely — and only
  over the seconds they are actually near it.
* **The cone has to be wider than the field.** A LEO satellite covers up to
  ~2.5 deg/s, so between two coarse samples it can cross the whole field and be
  gone. The guard radius is derived from the coarse step for that reason and is
  not separately configurable: the two are one decision.

Frames: SGP4 returns TEME, and the sensor WCS is equinox of date (INDI's
property is ``EQUATORIAL_EOD_COORD``). TEME differs from true-equator-of-date by
the equation of the equinoxes, up to ~1.1 arcsec — three orders of magnitude
below the arcminute-scale pointing error a TLE a few days old already carries,
and so ignored here rather than corrected.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.wcs import WCS

from ..config import Site
from ..sky.render import Optics, _convolve_fft, _splat, magnitude_to_electrons, make_psf
from ..sky.wcs import fast_lst_deg
from .config import SatellitesConfig
from .tle import TleRecord, load_source

log = logging.getLogger("astroskysim.satellites")

#: WGS84. The flattening matters at the 20 km level in observer position, which
#: is arcseconds of parallax on a LEO satellite - small, but free to get right.
_EARTH_A_KM = 6378.137
_EARTH_F = 1.0 / 298.257223563
_EARTH_E2 = _EARTH_F * (2.0 - _EARTH_F)
#: Mean radius, used for the shadow cylinder.
EARTH_RADIUS_KM = 6371.0
AU_KM = 149597870.7

#: Fastest a satellite can sweep the sky: ~7.8 km/s across a ~180 km slant range
#: is the practical ceiling for anything that stays in orbit for a pass. The
#: search cone is this times the coarse step, so nothing crosses the field
#: between two coarse samples unnoticed.
MAX_ANGULAR_RATE_DEG_S = 2.5

#: Target motion between two finely propagated points. The path between them is
#: drawn as a straight line, and a great circle sagitta over 200 px at these
#: plate scales is ~0.04 px - far below the PSF, and 20x cheaper than sampling
#: every pixel with SGP4.
TARGET_PX_PER_SAMPLE = 200.0
#: Spacing of the sub-pixel points splatted along each straight segment.
SUBSAMPLE_PX = 0.4
_MIN_STEP_S = 0.005
_MAX_STEP_S = 2.0
_MAX_FINE_SAMPLES = 4000
_MAX_SUBSAMPLES = 200_000
#: Working-set ceiling for one chunk of the coarse search. 48 bytes per
#: satellite-sample is the position and velocity SGP4 returns for it.
_COARSE_CHUNK_BYTES = 64 << 20


@dataclass(frozen=True, slots=True)
class Trail:
    """One satellite's contribution to one frame."""

    name: str
    #: Apparent magnitude at closest approach to the frame centre.
    mag: float
    #: Length of the drawn path inside the frame, in pixels.
    length_px: float


def observer_teme_km(site: Site, jd: np.ndarray | float) -> np.ndarray:
    """Observer position in TEME, km. Shape ``(..., 3)``, broadcasting over jd.

    The observer's right ascension *is* the local sidereal time, so the geodetic
    -> ECEF conversion and the rotation into the inertial frame collapse into
    one expression. ``fast_lst_deg`` is pure arithmetic and vectorises.
    """
    lst = np.deg2rad(fast_lst_deg(np.asarray(jd, dtype=float), site.longitude))
    lat = math.radians(site.latitude)
    h = site.elevation / 1000.0
    n = _EARTH_A_KM / math.sqrt(1.0 - _EARTH_E2 * math.sin(lat) ** 2)
    r_xy = (n + h) * math.cos(lat)
    z = np.full_like(lst, (n * (1.0 - _EARTH_E2) + h) * math.sin(lat))
    return np.stack([r_xy * np.cos(lst), r_xy * np.sin(lst), z], axis=-1)


def sun_teme_km(jd: np.ndarray | float) -> np.ndarray:
    """Geocentric solar position, km, mean equinox of date. Shape ``(..., 3)``.

    The USNO low-precision series: good to ~0.01 deg, which is four orders of
    magnitude better than the shadow test needs and costs no ephemeris, no IERS
    table and no astropy ``Time``. ``tests/test_satellites.py`` pins it against
    ``astropy.coordinates.get_sun``.
    """
    d = np.asarray(jd, dtype=float) - 2451545.0
    mean_lon = np.deg2rad(280.460 + 0.9856474 * d)
    anomaly = np.deg2rad(357.528 + 0.9856003 * d)
    ecl_lon = mean_lon + np.deg2rad(1.915) * np.sin(anomaly) + np.deg2rad(0.020) * np.sin(2 * anomaly)
    obliquity = np.deg2rad(23.439 - 4.0e-7 * d)
    dist = (1.00014 - 0.01671 * np.cos(anomaly) - 0.00014 * np.cos(2 * anomaly)) * AU_KM
    return np.stack(
        [
            dist * np.cos(ecl_lon),
            dist * np.cos(obliquity) * np.sin(ecl_lon),
            dist * np.sin(obliquity) * np.sin(ecl_lon),
        ],
        axis=-1,
    )


def is_sunlit(r_sat_km: np.ndarray, sun_km: np.ndarray) -> np.ndarray:
    """Whether the Sun reaches each satellite, as a cylindrical umbra test.

    Sunward of the Earth's centre is always lit; anti-sunward it is lit when it
    misses the Earth's shadow cylinder. The real shadow is a cone with a
    penumbra a few hundred km deep, so a satellite entering eclipse fades over a
    second or two where this cuts sharply. That is the wrong detail to chase:
    the useful behaviour - a trail that simply stops mid-frame - is already here.
    """
    sun_hat = sun_km / np.linalg.norm(sun_km, axis=-1, keepdims=True)
    along = np.sum(r_sat_km * sun_hat, axis=-1)
    perp_sq = np.sum(r_sat_km * r_sat_km, axis=-1) - along * along
    return (along >= 0.0) | (perp_sq > EARTH_RADIUS_KM**2)


def apparent_magnitude(
    std_mag: np.ndarray | float, range_km: np.ndarray | float, phase_rad: np.ndarray | float
) -> np.ndarray:
    """Visual magnitude from the standard magnitude, range and phase angle.

    The standard magnitude is defined at 1000 km and a 90 deg phase angle, so
    this is the inverse-square term plus a diffuse-sphere phase function
    normalised to unity there:

    >>> float(np.round(apparent_magnitude(5.9, 1000.0, np.pi / 2), 6))
    5.9

    Halving the range brightens it by 1.5 magnitudes; a satellite overhead near
    local midnight (small phase angle, fully lit face towards the observer) adds
    most of another magnitude on top.
    """
    phase = np.clip(np.asarray(phase_rad, dtype=float), 0.0, np.pi)
    # Diffuse sphere: F(90 deg) = 1/pi, so pi*F normalises to 1 at the standard
    # phase angle and the formula reduces to the range term alone.
    f = (np.sin(phase) + (np.pi - phase) * np.cos(phase)) / np.pi
    f = np.maximum(f, 1e-9)
    dist = np.maximum(np.asarray(range_km, dtype=float), 1e-6)
    return np.asarray(std_mag, dtype=float) + 5.0 * np.log10(dist / 1000.0) - 2.5 * np.log10(np.pi * f)


def _to_radec(vec: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(ra_deg, dec_deg, range) from a topocentric cartesian vector."""
    dist = np.linalg.norm(vec, axis=-1)
    ra = np.rad2deg(np.arctan2(vec[..., 1], vec[..., 0])) % 360.0
    dec = np.rad2deg(np.arcsin(np.clip(vec[..., 2] / np.maximum(dist, 1e-9), -1.0, 1.0)))
    return ra, dec, dist


def _clip_segment(
    x0: float, y0: float, x1: float, y1: float, lo_x: float, lo_y: float, hi_x: float, hi_y: float
) -> tuple[float, float] | None:
    """Liang-Barsky: the parameter range of a segment inside a box, or None.

    Clipping before subdividing is what keeps a 60000 px path affordable: only
    the few thousand pixels that land on the sensor are ever splatted, and the
    sub-pixel spacing stays constant instead of degrading into a dotted line
    once a fixed subdivision budget is spread over the whole path.
    """
    t0, t1 = 0.0, 1.0
    dx, dy = x1 - x0, y1 - y0
    for p, q in ((-dx, x0 - lo_x), (dx, hi_x - x0), (-dy, y0 - lo_y), (dy, hi_y - y0)):
        if p == 0.0:
            if q < 0.0:
                return None
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return None
            t0 = max(t0, r)
        else:
            if r < t0:
                return None
            t1 = min(t1, r)
    return (t0, t1) if t1 > t0 else None


def _windows(flags: np.ndarray, times: np.ndarray) -> list[tuple[float, float]]:
    """Contiguous runs of ``flags``, widened by one sample on each side.

    The widening is not slack: a run is where the satellite was *sampled* near
    the field, and it was already on its way in one step earlier.
    """
    idx = np.flatnonzero(flags)
    if idx.size == 0:
        return []
    groups = np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1)
    last = len(times) - 1
    return [
        (float(times[max(int(g[0]) - 1, 0)]), float(times[min(int(g[-1]) + 1, last)]))
        for g in groups
    ]


class SatelliteSky:
    """Every loaded satellite, and the trails they draw on a frame."""

    def __init__(
        self,
        records: list[TleRecord],
        satrecs: list,
        std_mag: np.ndarray,
        site: Site,
        cfg: SatellitesConfig,
    ) -> None:
        from sgp4.api import SatrecArray

        self.names = [r.name for r in records]
        self.satrecs = satrecs
        self.array = SatrecArray(satrecs)
        self.std_mag = std_mag
        self.site = site
        self.cfg = cfg
        self._single = SatrecArray  # constructor, for the per-candidate fine pass

    def __len__(self) -> int:
        return len(self.satrecs)

    @property
    def guard_deg(self) -> float:
        """Half-angle of the search cone around the field centre."""
        return MAX_ANGULAR_RATE_DEG_S * self.cfg.coarse_step_s

    # -- the two passes ----------------------------------------------------
    def _candidates(self, jd_start: float, jd_end: float, ra: float, dec: float, radius_deg: float):
        """Objects whose line of sight enters the search cone, and when.

        Chunked over *time* rather than propagated in one call, because the
        result of one is ``nsat x nsamples x 3`` doubles twice over (position
        and velocity). A 300 s sub over 12000 objects is 35 MB and unremarkable;
        an hour-long narrowband sub over the same sky is 400 MB, which is a
        surprising amount of memory for a feature nobody switched on. Chunking
        makes the peak depend on the number of satellites and not on how long
        the shutter was open.
        """
        exposure_s = max((jd_end - jd_start) * 86400.0, 0.0)
        n = max(2, int(math.ceil(exposure_s / self.cfg.coarse_step_s)) + 1)
        times = np.linspace(jd_start, jd_end, n)
        observer = observer_teme_km(self.site, times)
        day = float(math.floor(jd_start))
        pointing = np.array(
            [
                math.cos(math.radians(dec)) * math.cos(math.radians(ra)),
                math.cos(math.radians(dec)) * math.sin(math.radians(ra)),
                math.sin(math.radians(dec)),
            ]
        )
        cos_limit = math.cos(math.radians(min(radius_deg + self.guard_deg, 179.9)))

        chunk = max(1, min(n, _COARSE_CHUNK_BYTES // max(len(self) * 48, 1)))
        near = np.zeros((len(self), n), dtype=bool)
        for a in range(0, n, chunk):
            b = min(a + chunk, n)
            errors, positions, _ = self.array.sgp4(
                np.full(b - a, day), times[a:b] - day
            )  # (nsat, m) and (nsat, m, 3), km, TEME
            positions -= observer[a:b][None, :, :]
            dist = np.linalg.norm(positions, axis=2)
            cos_sep = (positions @ pointing) / np.maximum(dist, 1e-9)
            near[:, a:b] = (cos_sep > cos_limit) & (errors == 0)
        return np.flatnonzero(near.any(axis=1)), near, times

    def _fine_track(self, index: int, jd_a: float, jd_b: float, scale_arcsec_px: float):
        """Positions, ranges and magnitudes of one satellite over one window.

        The sample step is chosen from the satellite's own speed and range, so a
        Starlink overhead is sampled 20x more finely than a geostationary
        satellite and neither is sampled more than it needs.
        """
        one = self._single([self.satrecs[index]])
        span_s = max((jd_b - jd_a) * 86400.0, 1e-6)
        day = math.floor(jd_a)

        probe_t = np.linspace(jd_a, jd_b, 8)
        _, probe_r, probe_v = one.sgp4(np.full(8, float(day)), probe_t - day)
        rel = probe_r[0] - observer_teme_km(self.site, probe_t)
        rng = np.maximum(np.linalg.norm(rel, axis=-1), 1.0)
        # Overestimate: the observer's own 0.46 km/s is added rather than
        # resolved, so the step is short enough at every geometry.
        rate_arcsec_s = np.max(
            np.rad2deg((np.linalg.norm(probe_v[0], axis=-1) + 0.5) / rng) * 3600.0
        )
        step_s = float(
            np.clip(
                TARGET_PX_PER_SAMPLE * scale_arcsec_px / max(rate_arcsec_s, 1e-6),
                _MIN_STEP_S,
                _MAX_STEP_S,
            )
        )
        n = int(min(max(math.ceil(span_s / step_s) + 1, 2), _MAX_FINE_SAMPLES))

        times = np.linspace(jd_a, jd_b, n)
        errors, r, _ = one.sgp4(np.full(n, float(day)), times - day)
        geocentric = r[0]
        topo = geocentric - observer_teme_km(self.site, times)
        ra, dec, dist = _to_radec(topo)

        sun = sun_teme_km(times)
        to_sun = sun - geocentric
        to_obs = -topo
        cos_phase = np.sum(to_sun * to_obs, axis=-1) / np.maximum(
            np.linalg.norm(to_sun, axis=-1) * np.linalg.norm(to_obs, axis=-1), 1e-9
        )
        mag = apparent_magnitude(
            self.std_mag[index], dist, np.arccos(np.clip(cos_phase, -1.0, 1.0))
        )
        valid = errors[0] == 0
        if self.cfg.require_sunlit:
            valid &= is_sunlit(geocentric, sun)
        # A sample that is not drawn must not be able to win the "brightest point
        # of this trail" comparison with whatever a failed propagation produced.
        return ra, dec, np.where(valid, mag, np.inf), valid, times

    # -- the frame ---------------------------------------------------------
    def render(
        self,
        *,
        wcs: WCS,
        shape: tuple[int, int],
        optics: Optics,
        exposure_s: float,
        jd_end: float,
    ) -> tuple[np.ndarray, list[Trail]]:
        """Electrons from satellite trails, shaped exactly ``shape``.

        Returns the plane and one ``Trail`` per satellite that actually reached
        the sensor, which is what the frame count in the FITS header reports.
        """
        out = np.zeros(shape, dtype=np.float64)
        if exposure_s <= 0.0 or not len(self):
            return out, []

        height, width = shape
        jd_start = jd_end - exposure_s / 86400.0
        ra_c, dec_c = float(wcs.wcs.crval[0]), float(wcs.wcs.crval[1])
        scale = optics.scale_arcsec_px
        radius_deg = 0.5 * math.hypot(width, height) * scale / 3600.0

        candidates, near, times = self._candidates(jd_start, jd_end, ra_c, dec_c, radius_deg)
        if candidates.size == 0:
            return out, []

        psf = make_psf(optics)
        pad = max(psf.shape) // 2 + 2
        lo_x, lo_y = -float(pad), -float(pad)
        hi_x, hi_y = float(width + pad), float(height + pad)

        xs: list[np.ndarray] = []
        ys: list[np.ndarray] = []
        ws: list[np.ndarray] = []
        trails: list[Trail] = []

        for index in candidates:
            drawn_px = 0.0
            best_mag = math.inf
            for jd_a, jd_b in _windows(near[index], times):
                ra, dec, mag, valid, sample_t = self._fine_track(int(index), jd_a, jd_b, scale)
                if not valid.any():
                    continue
                x, y = wcs.wcs_world2pix(ra, dec, 0)
                rate = magnitude_to_electrons(mag, optics, 1.0)
                rate[~valid] = 0.0
                dt_s = float(np.diff(sample_t).mean()) * 86400.0

                for j in range(len(x) - 1):
                    weight = 0.5 * (rate[j] + rate[j + 1])
                    if weight <= 0.0:
                        continue
                    clipped = _clip_segment(
                        x[j], y[j], x[j + 1], y[j + 1], lo_x, lo_y, hi_x, hi_y
                    )
                    if clipped is None:
                        continue
                    t0, t1 = clipped
                    dx, dy = x[j + 1] - x[j], y[j + 1] - y[j]
                    seg_px = math.hypot(dx, dy) * (t1 - t0)
                    n_sub = int(min(max(math.ceil(seg_px / SUBSAMPLE_PX), 1), _MAX_SUBSAMPLES))
                    frac = (np.arange(n_sub) + 0.5) / n_sub * (t1 - t0) + t0
                    sx = x[j] + dx * frac
                    sy = y[j] + dy * frac
                    xs.append(sx)
                    ys.append(sy)
                    ws.append(np.full(n_sub, weight * dt_s * (t1 - t0) / n_sub))
                    inside = (sx >= 0) & (sx < width) & (sy >= 0) & (sy < height)
                    if inside.any():
                        drawn_px += seg_px * float(inside.mean())
                        best_mag = min(best_mag, float(min(mag[j], mag[j + 1])))
            if drawn_px > 0.0:
                trails.append(Trail(self.names[int(index)], best_mag, drawn_px))

        if not xs:
            return out, []

        plane = _splat(
            (height + 2 * pad, width + 2 * pad),
            np.concatenate(xs) + pad,
            np.concatenate(ys) + pad,
            np.concatenate(ws),
        )
        # Same PSF as the stars: a trail through a defocused frame is a
        # defocused trail, and one convolution covers every satellite at once.
        convolved = _convolve_fft(plane, psf)[pad : pad + height, pad : pad + width]
        if trails:
            log.debug(
                "%d satellite trail(s): %s",
                len(trails),
                ", ".join(f"{t.name} mag {t.mag:.1f} over {t.length_px:.0f} px" for t in trails),
            )
        return np.clip(convolved, 0.0, None), trails


def build_satellite_sky(
    cfg: SatellitesConfig, site: Site, jd_now: float
) -> SatelliteSky | None:
    """Load every enabled source, or explain why there is nothing to load.

    Returns ``None`` rather than raising for every ordinary reason a fresh
    checkout has no satellites - the extra is not installed, nothing has been
    fetched yet - because a missing element set must not stop a simulator that
    otherwise works. Each case logs the one command that fixes it.
    """
    if not cfg.enabled:
        return None
    try:
        from sgp4.api import Satrec
    except ImportError:
        log.warning(
            "satellites are enabled but the sgp4 package is missing; install the "
            "extra (uv sync --all-extras) or set enabled = false in the satellite config"
        )
        return None

    records: list[TleRecord] = []
    std_mags: list[float] = []
    loaded: list[str] = []
    missing: list[str] = []
    for src in cfg.active_sources:
        found = load_source(src, Path(cfg.tle_dir))
        if not found:
            missing.append(src.key)
            continue
        records.extend(found)
        std_mags.extend([cfg.std_mag_for(src)] * len(found))
        loaded.append(f"{src.key} ({len(found)})")

    if missing:
        log.info(
            "no elements yet for %s - run `astroskysim fetch-satellites`", ", ".join(missing)
        )
    if not records:
        return None

    # De-duplicate on the NORAD id: `active` overlaps almost every other list,
    # and a satellite loaded twice draws its trail twice, at double brightness.
    seen: dict[str, int] = {}
    satrecs: list = []
    keep_mag: list[float] = []
    keep_records: list[TleRecord] = []
    bad = 0
    for record, mag in zip(records, std_mags, strict=True):
        norad = record.line1[2:7].strip()
        if norad in seen:
            continue
        try:
            satrec = Satrec.twoline2rv(record.line1, record.line2)
        except (ValueError, RuntimeError):
            bad += 1
            continue
        seen[norad] = len(satrecs)
        satrecs.append(satrec)
        keep_mag.append(mag)
        keep_records.append(record)

    if bad:
        log.warning("%d element set(s) would not parse and were skipped", bad)
    if len(satrecs) > cfg.max_satellites:
        log.warning(
            "%d objects exceeds max_satellites = %d; the last %d are not in the sky",
            len(satrecs),
            cfg.max_satellites,
            len(satrecs) - cfg.max_satellites,
        )
        satrecs = satrecs[: cfg.max_satellites]
        keep_mag = keep_mag[: cfg.max_satellites]
        keep_records = keep_records[: cfg.max_satellites]

    if not satrecs:
        return None

    # Median, not newest. Element epochs are individually noisy - a high orbit is
    # often issued with an epoch a day or two in the *future* - so the newest one
    # can sit ahead of now while eight hundred others are a fortnight stale, and
    # the staleness check would never fire.
    epochs = np.array([s.jdsatepoch + s.jdsatepochF for s in satrecs])
    age_days = float(jd_now - np.median(epochs))
    if age_days > cfg.max_age_days:
        log.warning(
            "orbital elements are a median %.1f days old (limit %.0f): trails will be "
            "plausible but misplaced. Run `astroskysim fetch-satellites --force`",
            age_days,
            cfg.max_age_days,
        )
    log.info(
        "satellites: %d objects from %s, elements a median %.1f days old",
        len(satrecs),
        ", ".join(loaded),
        age_days,
    )
    return SatelliteSky(keep_records, satrecs, np.asarray(keep_mag, dtype=float), site, cfg)
