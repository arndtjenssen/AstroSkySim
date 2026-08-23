"""One survey per filter, and the two rules that make it mean anything.

Attaching Ha to an Ha survey is the easy half. The half that decides whether the
frames are worth anything:

* an in-band survey is exempt from the filter's broadband transmission, or a
  narrowband sub arrives fifty times too faint and never beats luminance;
* an absolute ``ref_value`` anchors a linear survey once, so its bands keep
  their real ratio and a blank field stays blank - where the per-frame
  percentile makes every band and every target equally bright.
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
from astroskysim.sources.base import RenderContext, calibrate_survey_image
from astroskysim.sources.dss import DssSource
from astroskysim.sources.filtered import FilterSurveySource

SHAPE = (60, 60)


def optics(throughput: float = 0.5) -> Optics:
    return Optics(aperture_mm=90.0, scale_arcsec_px=1.795, throughput=throughput)


SURVEY_SCALE = 1.7  # arcsec/px of the fixture cutout


def survey_bytes(
    peak: float = 1000.0,
    pedestal: float = 100.0,
    pixels: int = 200,
    sigma_arcsec: float | None = None,
) -> bytes:
    """A smooth blob of amplitude ``peak`` on an instrumental pedestal."""
    y, x = np.mgrid[0:pixels, 0:pixels].astype(float)
    c = pixels / 2
    sigma = pixels / 6 if sigma_arcsec is None else sigma_arcsec / SURVEY_SCALE
    data = pedestal + peak * np.exp(-(((x - c) ** 2 + (y - c) ** 2) / (2 * sigma**2)))
    w = WCS(naxis=2)
    w.wcs.crpix = [c, c]
    w.wcs.crval = [83.6, 22.0]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cdelt = [-1.7 / 3600.0, 1.7 / 3600.0]
    buf = io.BytesIO()
    fits.PrimaryHDU(data=data, header=w.to_header()).writeto(buf)
    return buf.getvalue()


def ctx(
    exposure_s: float = 10.0,
    opt: Optics | None = None,
    filter_name: str | None = None,
    filter_transmission: float = 1.0,
) -> RenderContext:
    opt = opt or optics()
    return RenderContext(
        wcs=sensor_wcs(83.6, 22.0, SHAPE[1], SHAPE[0], opt.scale_arcsec_px),
        shape=SHAPE,
        optics=opt,
        exposure_s=exposure_s,
        rng=np.random.default_rng(7),
        filter_name=filter_name,
        filter_transmission=filter_transmission,
    )


def fake_dss(
    monkeypatch, peak: float = 1000.0, sigma_arcsec: float | None = None, **kw
) -> DssSource:
    src = DssSource(survey="eso:DSS", **kw)
    raw = survey_bytes(peak=peak, sigma_arcsec=sigma_arcsec)
    monkeypatch.setattr(src, "_fetch_eso", lambda *a, **k: raw)
    return src


class Marker:
    """A stand-in source that returns a constant, so dispatch is observable."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.name = f"marker{value:g}"

    def render(self, c: RenderContext) -> np.ndarray:
        return np.full(c.shape, self.value, dtype=np.float64)


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------
def test_each_filter_gets_its_own_survey():
    src = FilterSurveySource(Marker(1.0), {"Ha": Marker(7.0), "OIII": Marker(3.0)})
    assert src.render(ctx(filter_name="Ha")).max() == 7.0
    assert src.render(ctx(filter_name="OIII")).max() == 3.0


def test_an_unmapped_filter_falls_back_to_the_default_survey():
    src = FilterSurveySource(Marker(1.0), {"Ha": Marker(7.0)})
    assert src.render(ctx(filter_name="L")).max() == 1.0


def test_the_guide_camera_never_picks_up_a_per_filter_survey():
    """``filter_name is None`` is the guider: its prism is upstream of the wheel.

    A narrowband layer reaching the guide camera would leave it with a field of
    three stars the moment a sequence switched to Ha.
    """
    src = FilterSurveySource(Marker(1.0), {"Ha": Marker(7.0)})
    assert src.render(ctx(filter_name=None)).max() == 1.0


def test_the_rig_tells_the_source_which_filter_is_in_the_beam():
    cfg = Config()
    cfg.filter_wheel.names = ["L", "R", "G", "B", "Ha"]
    cfg.filter_wheel.focus_offsets = [0, 0, 0, 0, 120]
    cfg.filter_wheel.transmission = [1.0, 0.3, 0.3, 0.3, 0.02]
    cfg.sensor.width_px = cfg.sensor.height_px = 64
    rig = build_rig(cfg)

    seen: list[tuple[str | None, float]] = []

    class Spy:
        name = "spy"

        def render(self, c):
            seen.append((c.filter_name, c.filter_transmission))
            return np.zeros(c.shape)

    rig.source = Spy()
    rig.filter.slot = 5  # Ha
    rig.capture(rig.camera)
    rig.capture(rig.guider)
    assert seen == [("Ha", pytest.approx(0.02)), (None, 1.0)]


def test_a_per_filter_key_must_name_a_real_filter():
    """A typo would otherwise leave that filter quietly on the default survey."""
    with pytest.raises(ValueError, match="no such filter: Halpha"):
        Config.model_validate(
            {
                "filter_wheel": {"names": ["L", "Ha"], "focus_offsets": [0, 120]},
                "source": {"dss": {"per_filter": {"Halpha": {"survey": "hips:ha"}}}},
            }
        )


def test_the_registry_builds_one_source_per_mapped_filter():
    from astroskysim.sources.registry import build_source

    cfg = Config.model_validate(
        {
            "filter_wheel": {"names": ["L", "Ha", "OIII"], "focus_offsets": [0, 120, 120]},
            "source": {
                "mode": "dss",
                "dss": {
                    "fallback_to_artificial": False,
                    "per_filter": {
                        "Ha": {"survey": "hips:ha", "ref_value": 1000.0},
                        "OIII": {"survey": "hips:oiii", "ref_value": 1000.0},
                    },
                },
            },
        }
    )
    src = build_source(cfg)
    assert isinstance(src, FilterSurveySource)
    assert src.per_filter["Ha"].survey == "hips:ha"
    assert src.per_filter["Ha"].ref_value == 1000.0
    # Per-filter layers are in-band by default; the shared default layer is not.
    assert src.per_filter["Ha"].in_band is True
    assert src.default.in_band is False


# --------------------------------------------------------------------------
# in-band: the filter does not dim its own band
# --------------------------------------------------------------------------
def test_an_in_band_survey_ignores_the_filter_transmission(monkeypatch):
    """Ha through a 3 nm Ha filter is not 2% of Ha."""
    src = fake_dss(monkeypatch, in_band=True)
    # throughput carries the 0.02, exactly as build_optics hands it over.
    narrow = src.render(ctx(opt=optics(0.5 * 0.02), filter_transmission=0.02))
    clear = src.render(ctx(opt=optics(0.5), filter_transmission=1.0))
    assert narrow.sum() == pytest.approx(clear.sum(), rel=1e-9)


def test_a_broadband_survey_is_still_dimmed_by_a_narrow_filter(monkeypatch):
    """The default layer is a stand-in, not a match, so it *is* attenuated."""
    src = fake_dss(monkeypatch, in_band=False)
    narrow = src.render(ctx(opt=optics(0.5 * 0.02), filter_transmission=0.02))
    clear = src.render(ctx(opt=optics(0.5), filter_transmission=1.0))
    assert narrow.sum() == pytest.approx(0.02 * clear.sum(), rel=1e-9)


def test_narrowband_keeps_the_nebula_and_loses_the_sky(monkeypatch):
    """The reason to own an Ha filter, as a ratio.

    Same nebula, same exposure: through Ha the object survives intact while the
    sky drops by the filter's transmission. Signal-to-sky therefore improves by
    1/transmission, which is what makes narrowband work under a bright sky.
    """
    src = fake_dss(monkeypatch, in_band=True, ref_value=1000.0, ref_mag_arcsec2=19.5)
    lum = src.render(ctx(opt=optics(0.5), filter_transmission=1.0)).max()
    ha = src.render(ctx(opt=optics(0.5 * 0.02), filter_transmission=0.02)).max()
    sky_lum = surface_brightness_to_electrons(21.0, optics(0.5)) * 10.0
    sky_ha = surface_brightness_to_electrons(21.0, optics(0.5 * 0.02)) * 10.0
    assert ha == pytest.approx(lum, rel=1e-9)
    assert (ha / sky_ha) / (lum / sky_lum) == pytest.approx(1 / 0.02, rel=1e-9)


# --------------------------------------------------------------------------
# absolute anchor: real band ratios, real target contrast
# --------------------------------------------------------------------------
def test_ref_value_anchors_absolutely_and_the_percentile_does_not():
    faint = np.array([[0.0, 1.0, 10.0, 100.0]])
    bright = faint * 4.0
    # Percentile: each frame normalised against itself, so both peak at ~1.
    assert calibrate_survey_image(bright).max() == pytest.approx(
        calibrate_survey_image(faint).max(), rel=1e-9
    )
    # Absolute: four times the signal is four times the answer.
    assert calibrate_survey_image(bright, ref_value=100.0).max() == pytest.approx(
        4.0 * calibrate_survey_image(faint, ref_value=100.0).max(), rel=1e-9
    )


def test_a_shared_ref_value_carries_the_real_line_ratio(monkeypatch):
    """Ha and OIII off one anchor keep the ratio the survey measured.

    NSNS cross-calibrates its three line maps, so this is the mechanism that
    makes IC 1805 come out Ha-dominated and M27 OIII-dominated without either
    being configured that way. With the percentile instead, both layers
    normalise against themselves and every line comes out equally bright - an
    SHO composite that looks right and means nothing.
    """
    anchor = dict(ref_value=1000.0, ref_mag_arcsec2=19.5, in_band=True)
    ha = fake_dss(monkeypatch, peak=1000.0, **anchor)
    oiii = fake_dss(monkeypatch, peak=166.0, **anchor)
    assert oiii.render(ctx()).max() / ha.render(ctx()).max() == pytest.approx(0.166, rel=0.02)

    percentile = dict(ref_percentile=99.0, ref_mag_arcsec2=19.5, in_band=True)
    ha_p = fake_dss(monkeypatch, peak=1000.0, **percentile)
    oiii_p = fake_dss(monkeypatch, peak=166.0, **percentile)
    assert oiii_p.render(ctx()).max() / ha_p.render(ctx()).max() == pytest.approx(1.0, rel=0.02)


def test_a_blank_field_stays_blank_under_an_absolute_anchor(monkeypatch):
    src = fake_dss(monkeypatch, peak=2.0, ref_value=1000.0, ref_mag_arcsec2=19.5, in_band=True)
    bright = fake_dss(monkeypatch, peak=2000.0, ref_value=1000.0, ref_mag_arcsec2=19.5, in_band=True)
    assert src.render(ctx()).max() < 0.01 * bright.render(ctx()).max()


def test_ref_value_survives_a_change_of_plate_scale(monkeypatch):
    """Reprojection preserves surface brightness, so the anchor is portable.

    A ``ref_value`` calibrated on one camera has to still mean the same surface
    brightness on another, or every sensor would need its own config.

    A compact blob on purpose: the *background* estimate is still per frame, so
    an object that fills the whole sensor has some of itself subtracted as sky
    and the anchor drifts. That is a property of the background step, not of the
    anchor, and it is why the estimator takes the median of the lower half.
    """
    src = fake_dss(
        monkeypatch, sigma_arcsec=8.0, ref_value=500.0, ref_mag_arcsec2=19.5, in_band=True
    )
    fine = src.render(ctx(opt=Optics(aperture_mm=90.0, scale_arcsec_px=1.0)))
    coarse = src.render(ctx(opt=Optics(aperture_mm=90.0, scale_arcsec_px=2.0)))
    # Four times the pixel area collects four times the light per pixel.
    assert coarse.max() == pytest.approx(4.0 * fine.max(), rel=0.05)
