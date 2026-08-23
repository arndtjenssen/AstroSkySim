"""Pin the closed-form hot path against astropy.

``rig.py`` uses ``fast_lst_deg`` / ``fast_radec_to_altaz`` on the simulation
tick because the astropy equivalents cost ~300 ms per tick, which starved the
loop. That is only a defensible trade if the fast versions actually agree with
astropy, so this module measures the disagreement rather than assuming it.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.time import Time

from astroskysim.sky.wcs import (
    fast_altaz_to_radec,
    fast_lst_deg,
    fast_radec_to_altaz,
    local_sidereal_time_deg,
    radec_to_altaz,
    sensor_wcs,
)

# Measured disagreement between fast_lst_deg and astropy is up to ~9", varying
# with date. Two causes, both understood and both acceptable here:
#
#   * fast_lst_deg computes *mean* sidereal time; astropy returns *apparent*.
#     The equation of the equinoxes between them is bounded by ~1.2".
#   * fast_lst_deg treats the JD as UT1, while it is really UTC. The true
#     difference UT1-UTC reaches +-0.9 s, i.e. +-13.5" of hour angle, and is not
#     computable without the IERS bulletins we deliberately do not fetch.
#
# The consequence is bounded: LST feeds only HORIZONTAL_COORD, TIME_LST and
# pier-side selection. It never enters the imaging path - the mount reports
# RA/Dec directly and the WCS is built from that - so no frame is displaced by
# it. If arcsecond alt/az ever matters, plumb a ut1_utc offset into the config.
LST_TOLERANCE_ARCSEC = 20.0

# Alt/az inherits the LST error (as an hour-angle offset) plus the refraction
# and polar-motion terms astropy models and we do not.
ALTAZ_TOLERANCE_ARCSEC = 60.0

JDS = [2460000.5, 2460123.25, 2460500.75, 2461000.0]
LONGITUDES = [0.0, 4.90, -75.0, 174.7]


@pytest.mark.parametrize("jd", JDS)
@pytest.mark.parametrize("lon", LONGITUDES)
def test_fast_lst_matches_astropy(jd, lon):
    t = Time(jd, format="jd", scale="utc")
    want = local_sidereal_time_deg(lon, t)
    got = fast_lst_deg(jd, lon)
    diff = abs((got - want + 180.0) % 360.0 - 180.0) * 3600.0
    assert diff < LST_TOLERANCE_ARCSEC, f"{diff:.2f}\" apart"


@pytest.mark.parametrize(
    ("ra", "dec"), [(83.6, 22.0), (0.0, 0.0), (200.0, -40.0), (350.0, 75.0)]
)
def test_fast_altaz_matches_astropy(ra, dec):
    """Compared without refraction, which neither implementation models."""
    jd, lat, lon = 2460123.25, 52.37, 4.90
    t = Time(jd, format="jd", scale="utc")
    want_az, want_alt = radec_to_altaz(ra, dec, lat, lon, t)
    got_az, got_alt = fast_radec_to_altaz(ra, dec, lat, local_sidereal_time_deg(lon, t))

    d_alt = abs(got_alt - want_alt) * 3600.0
    d_az = abs((got_az - want_az + 180.0) % 360.0 - 180.0) * 3600.0
    # Azimuth degenerates near the zenith, so weight it by cos(alt).
    d_az *= np.cos(np.deg2rad(want_alt))
    assert d_alt < ALTAZ_TOLERANCE_ARCSEC, f"alt {d_alt:.1f}\" apart"
    assert d_az < ALTAZ_TOLERANCE_ARCSEC, f"az {d_az:.1f}\" apart"


@pytest.mark.parametrize(
    ("az", "alt"), [(0.0, 45.0), (180.0, 30.0), (270.0, 10.0), (95.0, 70.0)]
)
def test_altaz_round_trip(az, alt):
    lat, lst = 52.37, 123.456
    ra, dec = fast_altaz_to_radec(az, alt, lat, lst)
    az2, alt2 = fast_radec_to_altaz(ra, dec, lat, lst)
    assert alt2 == pytest.approx(alt, abs=1e-6)
    assert (az2 - az + 180) % 360 - 180 == pytest.approx(0.0, abs=1e-6)


def test_fast_path_is_actually_fast():
    """The whole point of the closed form. Guards against a regression to
    astropy in the tick path."""
    import time

    t0 = time.perf_counter()
    for i in range(2000):
        lst = fast_lst_deg(2460000.5 + i * 1e-5, 4.9)
        fast_radec_to_altaz(83.6, 22.0, 52.37, lst)
    per_call_us = (time.perf_counter() - t0) / 2000 * 1e6
    assert per_call_us < 500, f"{per_call_us:.0f} us per LST+altaz pair"


# --------------------------------------------------------------------------
# Sensor WCS
# --------------------------------------------------------------------------
def test_sensor_wcs_centre_maps_to_pointing():
    w = sensor_wcs(83.6, 22.0, 200, 100, 2.0)
    ra, dec = w.wcs_pix2world([[(200 - 1) / 2.0, (100 - 1) / 2.0]], 0)[0]
    assert ra == pytest.approx(83.6, abs=1e-6)
    assert dec == pytest.approx(22.0, abs=1e-6)


def test_sensor_wcs_plate_scale():
    scale = 2.0
    w = sensor_wcs(83.6, 22.0, 200, 100, scale)
    (ra1, dec1), (ra2, dec2) = w.wcs_pix2world([[100, 50], [110, 50]], 0)
    sep = np.hypot((ra2 - ra1) * np.cos(np.deg2rad(dec1)), dec2 - dec1) * 3600.0
    assert sep == pytest.approx(10 * scale, rel=1e-3)


def test_sensor_wcs_ra_increases_leftward():
    """Standard sky orientation: east (increasing RA) is to the left."""
    w = sensor_wcs(83.6, 22.0, 200, 100, 2.0)
    ra_left, _ = w.wcs_pix2world([[90, 50]], 0)[0]
    ra_right, _ = w.wcs_pix2world([[110, 50]], 0)[0]
    assert ra_left > ra_right


def test_position_angle_rotates_the_field():
    a = sensor_wcs(83.6, 22.0, 200, 100, 2.0, position_angle_deg=0.0)
    b = sensor_wcs(83.6, 22.0, 200, 100, 2.0, position_angle_deg=90.0)
    # A point offset along +x should land in a different sky direction.
    ra_a, dec_a = a.wcs_pix2world([[150, 50]], 0)[0]
    ra_b, dec_b = b.wcs_pix2world([[150, 50]], 0)[0]
    assert abs(dec_a - dec_b) * 3600 > 10
