"""One photometric scale for stars, sky and survey nebulosity.

The bug these pin: the survey path used to percentile-stretch each cutout to
[0, 1] and multiply by a fixed 400 e-/px/s. Nothing about the telescope reached
it, so a 1 s sub of IC 1805 on a 90 mm refractor arrived with 30-150 e- per
pixel of "nebula" over a 21 e-/px/s sky - a frame whose histogram filled the
whole range in one second, where the real 20 s sub is a hair above the noise.

Everything now goes through ``surface_brightness_to_electrons``, i.e. through
the same zero point, aperture and throughput the catalogue stars use.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from astroskysim.config import Config
from astroskysim.rig import build_rig
from astroskysim.sky.render import Optics, surface_brightness_to_electrons
from astroskysim.sky.wcs import sensor_wcs
from astroskysim.sources.base import RenderContext
from astroskysim.sources.dss import DssSource

SHAPE = (80, 100)


def optics(aperture_mm: float = 90.0, scale: float = 1.795, throughput: float = 0.5) -> Optics:
    return Optics(aperture_mm=aperture_mm, scale_arcsec_px=scale, throughput=throughput)


def rig_cfg(**optics_kw) -> Config:
    """A 90 mm f/4.8 with 3.76 um pixels - 1.795"/px, as in examples/sim.toml."""
    cfg = Config()
    cfg.telescope.focal_length_mm = 432.0
    cfg.telescope.aperture_mm = 90.0
    cfg.sensor.pixel_size_um = 3.76
    cfg.sensor.width_px = cfg.sensor.height_px = 256
    for k, v in optics_kw.items():
        setattr(cfg.optics, k, v)
    return cfg


def survey_bytes(pixels: int = 300, scale_arcsec: float = 1.7, pedestal: float = 1000.0) -> bytes:
    """A smooth nebula on an instrumental pedestal, with its own WCS."""
    y, x = np.mgrid[0:pixels, 0:pixels].astype(float)
    c = pixels / 2
    data = pedestal + 1000.0 * np.exp(-(((x - c) ** 2 + (y - c) ** 2) / (2 * (pixels / 6) ** 2)))
    w = WCS(naxis=2)
    w.wcs.crpix = [c, c]
    w.wcs.crval = [83.6, 22.0]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    buf = io.BytesIO()
    fits.PrimaryHDU(data=data, header=w.to_header()).writeto(buf)
    return buf.getvalue()


def dss_ctx(exposure_s: float, opt: Optics) -> RenderContext:
    return RenderContext(
        wcs=sensor_wcs(83.6, 22.0, SHAPE[1], SHAPE[0], opt.scale_arcsec_px),
        shape=SHAPE,
        optics=opt,
        exposure_s=exposure_s,
        rng=np.random.default_rng(7),
    )


def fake_dss(monkeypatch, **kw) -> DssSource:
    src = DssSource(survey="eso:DSS", **kw)
    monkeypatch.setattr(src, "_fetch_eso", lambda *a, **k: survey_bytes())
    return src


# --------------------------------------------------------------------------
# the primitive
# --------------------------------------------------------------------------
def test_surface_brightness_matches_the_hand_calculation():
    """21 mag/arcsec^2 on a 90 mm at 1.795"/px, throughput 0.5, ZP 1e10."""
    area = np.pi * (0.045**2)
    mag_px = 21.0 - 2.5 * np.log10(1.795**2)
    want = 1.0e10 * area * 0.5 * 10 ** (-0.4 * mag_px)
    assert surface_brightness_to_electrons(21.0, optics()) == pytest.approx(want, rel=1e-9)
    assert surface_brightness_to_electrons(21.0, optics()) == pytest.approx(0.408, abs=0.002)


def test_one_magnitude_is_a_factor_of_2_512():
    a = surface_brightness_to_electrons(21.0, optics())
    b = surface_brightness_to_electrons(20.0, optics())
    assert b / a == pytest.approx(10**0.4, rel=1e-9)


def test_a_bigger_aperture_collects_more_sky():
    small = surface_brightness_to_electrons(21.0, optics(aperture_mm=90.0))
    big = surface_brightness_to_electrons(21.0, optics(aperture_mm=180.0))
    assert big / small == pytest.approx(4.0, rel=1e-9)


def test_a_coarser_pixel_collects_more_sky_per_pixel():
    """Surface brightness is per arcsec^2, so a pixel's share scales with area."""
    fine = surface_brightness_to_electrons(21.0, optics(scale=1.0))
    coarse = surface_brightness_to_electrons(21.0, optics(scale=2.0))
    assert coarse / fine == pytest.approx(4.0, rel=1e-9)


# --------------------------------------------------------------------------
# sky background on the rig
# --------------------------------------------------------------------------
def test_sky_comes_from_the_sqm_reading():
    rig = build_rig(rig_cfg(sky_mag_arcsec2=21.0))
    assert rig.sky_e_s(rig.camera) == pytest.approx(0.408, abs=0.002)


def test_a_brighter_sky_raises_the_background():
    rural = build_rig(rig_cfg(sky_mag_arcsec2=21.0)).sky_e_s
    city = build_rig(rig_cfg(sky_mag_arcsec2=18.0))
    assert city.sky_e_s(city.camera) / rural(build_rig(rig_cfg()).camera) == pytest.approx(
        10**1.2, rel=1e-6
    )


def test_raw_electron_override_wins_and_is_reported_as_an_sqm():
    """``sky_background = 21.0`` is not an SQM 21 sky - that is the trap."""
    rig = build_rig(rig_cfg(sky_background=21.0))
    assert rig.sky_e_s(rig.camera) == 21.0
    assert rig.equivalent_sqm(21.0) == pytest.approx(16.7, abs=0.2)


def test_the_guide_camera_gets_its_own_sky_rate():
    """A different plate scale means a different background per pixel."""
    cfg = rig_cfg()
    cfg.sensor_guide_cam = cfg.sensor.model_copy(update={"pixel_size_um": 7.52})
    rig = build_rig(cfg)
    assert rig.sky_e_s(rig.guider) == pytest.approx(4.0 * rig.sky_e_s(rig.camera), rel=1e-6)


# --------------------------------------------------------------------------
# filters
# --------------------------------------------------------------------------
def test_a_filter_attenuates_signal_and_sky_together():
    cfg = rig_cfg()
    cfg.filter_wheel.transmission = [1.0, 0.3, 0.3, 0.3, 0.01]
    rig = build_rig(cfg)

    rig.filter.slot = 1  # L
    lum_sky = rig.sky_e_s(rig.camera)
    lum_thr = rig.build_optics(rig.camera).throughput

    rig.filter.slot = 5  # Ha
    assert rig.sky_e_s(rig.camera) == pytest.approx(0.01 * lum_sky, rel=1e-9)
    assert rig.build_optics(rig.camera).throughput == pytest.approx(0.01 * lum_thr, rel=1e-9)


def test_the_filter_wheel_does_not_dim_the_guide_camera():
    """The OAG pickoff prism is upstream of the wheel, as ``guide_hfd`` says.

    A narrowband filter in the guide beam would starve the guider, and guiding
    would stop the moment a sequence switched to Ha.
    """
    cfg = rig_cfg()
    cfg.filter_wheel.transmission = [1.0, 0.3, 0.3, 0.3, 0.01]
    rig = build_rig(cfg)

    rig.filter.slot = 1
    lum = rig.sky_e_s(rig.guider)
    rig.filter.slot = 5
    assert rig.sky_e_s(rig.guider) == pytest.approx(lum, rel=1e-12)


def test_no_transmission_configured_means_no_losses():
    rig = build_rig(rig_cfg())
    assert rig.cfg.filter_wheel.transmission is None
    rig.filter.slot = 5
    assert rig.build_optics(rig.camera).throughput == pytest.approx(rig.cfg.optics.throughput)


def test_transmission_length_is_checked_against_the_filter_names():
    cfg = Config()
    with pytest.raises(ValueError, match="transmission has 2 entries"):
        cfg.filter_wheel.model_validate(
            {"names": ["L", "R", "G"], "focus_offsets": [0, 0, 0], "transmission": [1.0, 0.3]}
        )


# --------------------------------------------------------------------------
# the survey path is on the same scale
# --------------------------------------------------------------------------
def test_survey_signal_scales_with_exposure(monkeypatch):
    src = fake_dss(monkeypatch)
    short = src.render(dss_ctx(1.0, optics())).sum()
    long = src.render(dss_ctx(20.0, optics())).sum()
    assert long == pytest.approx(20.0 * short, rel=1e-9)


def test_survey_signal_scales_with_aperture(monkeypatch):
    """The regression: a fixed ``scale_e_s`` ignored the telescope entirely."""
    src = fake_dss(monkeypatch)
    small = src.render(dss_ctx(10.0, optics(aperture_mm=90.0))).sum()
    big = src.render(dss_ctx(10.0, optics(aperture_mm=180.0))).sum()
    assert big == pytest.approx(4.0 * small, rel=1e-9)


def test_survey_signal_scales_with_filter_transmission(monkeypatch):
    src = fake_dss(monkeypatch)
    clear = src.render(dss_ctx(10.0, optics(throughput=0.5))).sum()
    narrow = src.render(dss_ctx(10.0, optics(throughput=0.005))).sum()
    assert narrow == pytest.approx(0.01 * clear, rel=1e-9)


def test_dimmer_reference_surface_brightness_means_less_signal(monkeypatch):
    bright = fake_dss(monkeypatch, ref_mag_arcsec2=19.5).render(dss_ctx(10.0, optics())).sum()
    faint = fake_dss(monkeypatch, ref_mag_arcsec2=22.0).render(dss_ctx(10.0, optics())).sum()
    assert faint / bright == pytest.approx(10 ** (-0.4 * 2.5), rel=1e-9)


def test_a_one_second_sub_is_buried_in_the_noise(monkeypatch):
    """The complaint, as a test.

    On a 90 mm at 1.795"/px, one second of a 19.5 mag/arcsec^2 reference level
    is under two electrons - below the read noise of a decent CMOS. The old
    path put 30-150 e- there, which is what filled the histogram.
    """
    src = fake_dss(monkeypatch)
    one_second = src.render(dss_ctx(1.0, optics()))
    # examples/sim.toml reads out at 1.5 e-. Nebulosity across the frame has to
    # lose to that; only the survey's own bright cores may beat it.
    assert np.median(one_second) < 1.5, "a 1 s survey sub must be sub-read-noise"
    # The old path multiplied a [0, 1] stretch by a fixed 400 e-/px/s, which put
    # tens to hundreds of electrons here regardless of the telescope.
    assert np.median(one_second) < 400.0 / 50

    twenty = src.render(dss_ctx(20.0, optics()))
    assert np.median(twenty) > 3.0, "20 s must actually accumulate something"


def test_survey_and_sky_are_on_one_scale(monkeypatch):
    """Emission nebulosity sits near the natural sky, not far above it.

    A frame where the object outruns the sky by two orders of magnitude in one
    second is the failure mode; a factor of a few is what an SQM 21 site gives.
    """
    src = fake_dss(monkeypatch)
    opt = optics()
    frame = src.render(dss_ctx(20.0, opt))
    sky = surface_brightness_to_electrons(21.0, opt) * 20.0
    assert 0.05 < np.median(frame) / sky < 20.0
