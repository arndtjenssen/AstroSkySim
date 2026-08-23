"""World coordinate systems for the simulated sensor.

The sensor WCS is authoritative. Every image source must deliver pixels on
*this* grid — see ``astroskysim.sources``. Sensor geometry is immutable, which
rules out the tempting shortcut of resizing the sensor to match a survey's plate
scale: that mutates CCD_INFO underneath a connected client, and a client caches
those dimensions the moment it connects.
"""

from __future__ import annotations

import numpy as np
from astropy import units as u
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.wcs import WCS


def sensor_wcs(
    ra_deg: float,
    dec_deg: float,
    width_px: int,
    height_px: int,
    scale_arcsec_px: float,
    position_angle_deg: float = 0.0,
    flip_h: bool = False,
    flip_v: bool = False,
) -> WCS:
    """A gnomonic (TAN) WCS centred on the pointing.

    ``position_angle_deg`` rotates the field east of north, matching how a
    rotator's sky angle is normally reported.
    """
    w = WCS(naxis=2)
    # FITS is 1-indexed and CRPIX refers to the centre of the array.
    w.wcs.crpix = [(width_px + 1) / 2.0, (height_px + 1) / 2.0]
    w.wcs.crval = [ra_deg, dec_deg]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cunit = ["deg", "deg"]

    scale = scale_arcsec_px / 3600.0
    # RA increases to the left in a normal sky view, hence the negative x term.
    sx = -scale * (-1.0 if flip_h else 1.0)
    sy = scale * (-1.0 if flip_v else 1.0)

    theta = np.deg2rad(position_angle_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    w.wcs.cd = np.array(
        [
            [sx * cos_t, -sy * sin_t],
            [sx * sin_t, sy * cos_t],
        ]
    )
    w.array_shape = (height_px, width_px)
    return w


def frame_wcs(
    sensor: WCS, start_x: int, start_y: int, bin_x: int, bin_y: int, shape: tuple[int, int]
) -> WCS:
    """Re-reference a full-sensor WCS onto a subframed and/or binned readout.

    A binned pixel spans ``bin`` sensor pixels, so the plate scale grows by the
    bin factor and the reference pixel moves. Building the header WCS from the
    delivered array dimensions instead (as if it were an unbinned full frame)
    puts the tangent point in the wrong place for any subframe and reports the
    wrong scale for any binning, which sends a plate solve looking in the wrong
    field.
    """
    bin_x = max(int(bin_x), 1)
    bin_y = max(int(bin_y), 1)
    out = sensor.deepcopy()
    cx, cy = sensor.wcs.crpix
    out.wcs.crpix = [
        (cx - 1.0 - start_x - (bin_x - 1) / 2.0) / bin_x + 1.0,
        (cy - 1.0 - start_y - (bin_y - 1) / 2.0) / bin_y + 1.0,
    ]
    out.wcs.cd = sensor.wcs.cd * np.array([[bin_x, bin_y], [bin_x, bin_y]])
    out.array_shape = shape
    return out


def field_radius_deg(width_px: int, height_px: int, scale_arcsec_px: float) -> float:
    """Radius of the circle circumscribing the sensor, for catalogue queries."""
    w = width_px * scale_arcsec_px / 3600.0
    h = height_px * scale_arcsec_px / 3600.0
    return 0.5 * float(np.hypot(w, h))


def precess(
    ra_deg: float, dec_deg: float, from_epoch: str = "J2000", to_epoch: str | None = None
) -> tuple[float, float]:
    """Precess a position between epochs.

    Replaces the hand-coded Meeus 20.2-20.4 implementation (``precession5``).
    ``to_epoch=None`` means "now".
    """
    frame_in = "fk5" if from_epoch.startswith("J") else "icrs"
    c = SkyCoord(
        ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=frame_in, equinox=Time(from_epoch)
    )
    target = Time.now() if to_epoch is None else Time(to_epoch)
    out = c.transform_to(c.frame.replicate_without_data(equinox=target))
    return float(out.ra.deg), float(out.dec.deg)


def local_sidereal_time_deg(longitude_deg: float, when: Time | None = None) -> float:
    """Local apparent sidereal time in degrees. East longitude positive.

    Accurate but slow (builds a Time and consults IERS). Use ``fast_lst_deg`` on
    the simulation tick; ``tests/test_fast_astronomy.py`` pins the two together.
    """
    t = when or Time.now()
    lst = t.sidereal_time("apparent", longitude=longitude_deg * u.deg)
    return float(lst.deg)


# --------------------------------------------------------------------------
# Closed-form hot path.
#
# The simulation ticks at 10 Hz across six devices. Building an astropy Time and
# running sidereal_time / an AltAz transform per tick costs ~300 ms per tick,
# which starved the tick loop badly enough that slews crawled. These closed-form
# versions are ~10000x faster and agree with astropy to well under an arcsecond,
# which is far below the pointing errors we are simulating.
# --------------------------------------------------------------------------
def fast_lst_deg(jd_utc: float, longitude_deg: float) -> float:
    """Greenwich mean sidereal time plus east longitude, in degrees."""
    d = jd_utc - 2451545.0
    t = d / 36525.0
    gmst = (
        280.46061837
        + 360.98564736629 * d
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    )
    return (gmst + longitude_deg) % 360.0


def fast_radec_to_altaz(
    ra_deg: float, dec_deg: float, lat_deg: float, lst_deg: float
) -> tuple[float, float]:
    """(azimuth, altitude) in degrees; azimuth 0 at north, 180 at due south."""
    ha = np.deg2rad(lst_deg - ra_deg)
    dec = np.deg2rad(dec_deg)
    lat = np.deg2rad(lat_deg)
    sin_alt = np.sin(dec) * np.sin(lat) + np.cos(dec) * np.cos(lat) * np.cos(ha)
    alt = np.arcsin(np.clip(sin_alt, -1.0, 1.0))
    az = np.arctan2(
        -np.cos(dec) * np.sin(ha),
        np.sin(dec) * np.cos(lat) - np.cos(dec) * np.sin(lat) * np.cos(ha),
    )
    return float(np.rad2deg(az) % 360.0), float(np.rad2deg(alt))


def fast_altaz_to_radec(
    az_deg: float, alt_deg: float, lat_deg: float, lst_deg: float
) -> tuple[float, float]:
    """Inverse of ``fast_radec_to_altaz``."""
    az = np.deg2rad(az_deg)
    alt = np.deg2rad(alt_deg)
    lat = np.deg2rad(lat_deg)
    sin_dec = np.sin(alt) * np.sin(lat) + np.cos(alt) * np.cos(lat) * np.cos(az)
    dec = np.arcsin(np.clip(sin_dec, -1.0, 1.0))
    ha = np.arctan2(
        -np.cos(alt) * np.sin(az),
        np.sin(alt) * np.cos(lat) - np.cos(alt) * np.sin(lat) * np.cos(az),
    )
    ra = (lst_deg - np.rad2deg(ha)) % 360.0
    return float(ra), float(np.rad2deg(dec))


# Coordinates on the wire are equinox-of-date: INDI's property is literally
# EQUATORIAL_EOD_COORD. So the accurate reference implementations below must use
# FK5 at the epoch of observation, NOT ICRS. Labelling them ICRS silently adds
# the full J2000-to-now precession - about 20 arcminutes today - which is far
# larger than any error being modelled.
def radec_to_altaz(
    ra_deg: float, dec_deg: float, lat_deg: float, lon_deg: float, when: Time | None = None
) -> tuple[float, float]:
    """(azimuth, altitude) in degrees; azimuth 180 at due south.

    Input is equinox of date. Accurate but slow; ``fast_radec_to_altaz`` is the
    tick-path equivalent.
    """
    from astropy.coordinates import FK5, AltAz, EarthLocation

    t = when or Time.now()
    loc = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg)
    c = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=FK5(equinox=t))
    aa = c.transform_to(AltAz(obstime=t, location=loc))
    return float(aa.az.deg), float(aa.alt.deg)


def altaz_to_radec(
    az_deg: float, alt_deg: float, lat_deg: float, lon_deg: float, when: Time | None = None
) -> tuple[float, float]:
    """Inverse of ``radec_to_altaz``, returning equinox-of-date coordinates."""
    from astropy.coordinates import FK5, AltAz, EarthLocation

    t = when or Time.now()
    loc = EarthLocation(lat=lat_deg * u.deg, lon=lon_deg * u.deg)
    aa = SkyCoord(
        az=az_deg * u.deg, alt=alt_deg * u.deg, frame=AltAz(obstime=t, location=loc)
    )
    eod = aa.transform_to(FK5(equinox=t))
    return float(eod.ra.deg), float(eod.dec.deg)
