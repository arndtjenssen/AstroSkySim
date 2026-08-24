"""Image sources, including the three-mode contract.

The central invariant: **every source returns pixels on the sensor grid**, never
its own. Asserted here against three very different survey plate scales, because
the failure mode - a survey silently resizing the sensor - looks like a client
bug rather than a server one.

The DSS tests use a locally generated FITS cutout with its own deliberately
mismatched WCS instead of hitting the network, so they assert the reprojection
rather than the survey.
"""

from __future__ import annotations

import io
import math
import urllib.error
import urllib.parse

import numpy as np
import pytest
from astropy.io import fits
from astropy.wcs import WCS

from astroskysim.config import Config, SourceMode
from astroskysim.sky.catalog import SyntheticCatalog
from astroskysim.sky.render import Optics
from astroskysim.sky.wcs import sensor_wcs
from astroskysim.sources.artificial import ArtificialSource
from astroskysim.sources.base import RenderContext, SourceError, calibrate_survey_image
from astroskysim.sources.composite import CompositeSource, suppress_point_sources
from astroskysim.sources.dss import (
    CACHE_CELL_FRACTION,
    HIPS_ALIASES,
    HIPS_BASES,
    DssSource,
    FallbackSource,
    HipsEndpoint,
    resolve_hips_id,
    resolve_skyview_survey,
)
from astroskysim.sources.registry import build_source

SHAPE = (80, 100)  # (height, width)
SCALE = 3.0  # arcsec/px


def make_ctx(exposure_s: float = 5.0, ra: float = 83.6, dec: float = 22.0) -> RenderContext:
    return RenderContext(
        wcs=sensor_wcs(ra, dec, SHAPE[1], SHAPE[0], SCALE),
        shape=SHAPE,
        optics=Optics(aperture_mm=100.0, scale_arcsec_px=SCALE, seeing_arcsec=3.0),
        exposure_s=exposure_s,
        rng=np.random.default_rng(7),
    )


def survey_fits(
    *,
    pixels: int = 300,
    scale_arcsec: float = 1.7,
    ra=83.6,
    dec=22.0,
    finite_frac: float | None = None,
    planes: int = 0,
) -> bytes:
    """A fake survey cutout: coarse plate scale, different size, smooth nebula.

    ``finite_frac`` blanks everything outside a centred box of that fraction of
    the array, standing in for a survey footprint edge or a masked saturated
    core. ``planes`` produces an ``(planes, H, W)`` cube, as a colour HiPS does.
    """
    y, x = np.mgrid[0:pixels, 0:pixels].astype(float)
    c = pixels / 2
    data = 1000.0 * np.exp(-(((x - c) ** 2 + (y - c) ** 2) / (2 * (pixels / 6) ** 2)))
    if finite_frac is not None:
        if finite_frac <= 0.0:
            data[:] = np.nan
        else:
            half = pixels * finite_frac / 2.0
            data[(np.abs(x - c) > half) | (np.abs(y - c) > half)] = np.nan
    if planes:
        data = np.repeat(data[None, :, :], planes, axis=0)
    w = WCS(naxis=2)
    w.wcs.crpix = [c, c]
    w.wcs.crval = [ra, dec]
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    hdu = fits.PrimaryHDU(data=data, header=w.to_header())
    buf = io.BytesIO()
    hdu.writeto(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------
# artificial
# --------------------------------------------------------------------------
def test_artificial_returns_sensor_shaped_electrons():
    src = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    out = src.render(make_ctx())
    assert out.shape == SHAPE
    assert out.dtype == np.float64
    assert out.min() >= 0.0
    assert out.max() > 0.0, "no signal rendered"


def test_artificial_scales_with_exposure():
    src = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    short = src.render(make_ctx(exposure_s=1.0)).sum()
    long = src.render(make_ctx(exposure_s=10.0)).sum()
    assert long == pytest.approx(short * 10.0, rel=1e-6)


def test_artificial_is_deterministic_for_a_pointing():
    src = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    assert np.array_equal(src.render(make_ctx()), src.render(make_ctx()))


def test_limiting_magnitude_reduces_flux():
    bright = ArtificialSource(SyntheticCatalog(seed=1, limiting_mag=16.0), limiting_mag=8.0)
    faint = ArtificialSource(SyntheticCatalog(seed=1, limiting_mag=16.0), limiting_mag=16.0)
    assert faint.render(make_ctx()).sum() > bright.render(make_ctx()).sum()


# --------------------------------------------------------------------------
# dss
# --------------------------------------------------------------------------
def test_dss_reprojects_onto_the_sensor_grid(monkeypatch):
    """The ESO regression: a survey at a different scale and size must come back
    at exactly the sensor's dimensions."""
    src = DssSource(survey="eso:DSS")
    monkeypatch.setattr(src, "_fetch_eso", lambda *a, **k: survey_fits(pixels=300, scale_arcsec=1.7))
    out = src.render(make_ctx())
    assert out.shape == SHAPE, "source imposed its own geometry on the sensor"
    assert np.isfinite(out).all()
    assert out.max() > 0


@pytest.mark.parametrize("pixels,scale", [(120, 5.0), (300, 1.7), (700, 0.8)])
def test_sensor_geometry_is_invariant_to_survey_geometry(monkeypatch, pixels, scale):
    src = DssSource(survey="eso:DSS")
    monkeypatch.setattr(
        src, "_fetch_eso", lambda *a, **k: survey_fits(pixels=pixels, scale_arcsec=scale)
    )
    assert src.render(make_ctx()).shape == SHAPE


def test_dss_short_response_is_treated_as_an_error_page():
    """A body under 4096 bytes is an error page, and its text is the real
    diagnosis - so it must reach the caller."""
    src = DssSource(survey="eso:DSS")
    import urllib.request

    class FakeResp:
        def read(self):
            return b"ERROR: coordinates outside survey"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: FakeResp()
    try:
        with pytest.raises(SourceError, match="not an image.*coordinates outside"):
            src.fetch(make_ctx())
    finally:
        urllib.request.urlopen = orig


def test_dss_rejects_non_fits_response(monkeypatch):
    src = DssSource(survey="eso:DSS")
    monkeypatch.setattr(src, "_fetch_eso", lambda *a, **k: b"\x00" * 8192)
    with pytest.raises(SourceError, match="could not decode FITS"):
        src.fetch(make_ctx())


def test_unknown_survey_backend_is_rejected():
    with pytest.raises(SourceError, match="unknown survey backend"):
        DssSource(survey="hubble:everything").fetch(make_ctx())


def test_dss_cache_avoids_a_second_fetch(monkeypatch, tmp_path):
    src = DssSource(survey="eso:DSS", cache_dir=tmp_path)
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return survey_fits()

    monkeypatch.setattr(src, "_fetch_eso", fake)
    src.render(make_ctx())
    src.render(make_ctx())
    assert calls["n"] == 1, "cache did not prevent the second fetch"


def test_dss_cache_survives_tracking_noise(monkeypatch, tmp_path):
    """The centre comes from ``actual_pointing``, so it jitters by arcseconds
    between exposures. Keyed on the raw centre, the cache never hit once."""
    src = DssSource(survey="eso:DSS", cache_dir=tmp_path, mem_cache_size=0)
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return survey_fits()

    monkeypatch.setattr(src, "_fetch_eso", fake)
    jitter = np.random.default_rng(3).normal(0.0, 3.0 / 3600.0, size=(20, 2))
    for dra, ddec in jitter:
        src.render(make_ctx(ra=83.6 + dra, dec=22.0 + ddec))

    # 20 exposures, at most one cell boundary crossed by 3" of noise.
    assert calls["n"] <= 2, f"{calls['n']} downloads for one jittering field"
    assert len(list(tmp_path.glob("*.fits"))) <= 2


def test_dss_memory_cache_serves_repeat_exposures(monkeypatch):
    """No cache_dir configured still means one download per field, not per frame."""
    src = DssSource(survey="eso:DSS")
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return survey_fits()

    monkeypatch.setattr(src, "_fetch_eso", fake)
    for _ in range(3):
        src.render(make_ctx())
    assert calls["n"] == 1


def test_cache_grid_keeps_the_frame_inside_the_download():
    """Snapping displaces the centre, so the download must grow to compensate.

    Asserted as the invariant rather than for one pointing: wherever inside its
    cell the true centre lies, the requested footprint still contains the frame.
    """
    ctx = make_ctx()
    frame_deg = ctx.radius_deg * 2.4
    cell = frame_deg * CACHE_CELL_FRACTION
    rng = np.random.default_rng(11)
    for ra, dec in zip(rng.uniform(0, 360, 200), rng.uniform(-85, 85, 200), strict=True):
        ra_q, dec_q = DssSource._snap(ra, dec, cell)
        # Great-circle offset introduced by snapping.
        d_ra = ((ra - ra_q + 180.0) % 360.0 - 180.0) * math.cos(math.radians(dec))
        offset = math.hypot(d_ra, dec - dec_q)
        assert offset <= cell / 2.0 * math.sqrt(2) + 1e-9
        # The frame's circumscribing radius, displaced by the snap, still has to
        # sit inside the half-width of what we download.
        assert ctx.radius_deg + offset <= (frame_deg + cell) / 2.0 + 1e-9


def test_dss_cache_key_separates_the_two_cameras():
    """The requested pixel count is part of the response, so part of the key."""
    src = DssSource(survey="eso:DSS")
    main = src._cache_key(83.6, 22.0, 1.0, (1000, 1200))
    guide = src._cache_key(83.6, 22.0, 1.0, (480, 640))
    assert main != guide


def test_cache_dir_expands_a_tilde(tmp_path, monkeypatch):
    """``cache_dir = "~/.cache/astroskysim"`` used to create a directory named "~"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    src = DssSource(survey="eso:DSS", cache_dir="~/.cache/astroskysim")
    assert src.cache_dir == tmp_path / ".cache" / "astroskysim"
    assert src.cache_dir.is_dir()
    assert src.cache_dir.is_absolute(), "cache landed relative to the working directory"
    assert "~" not in str(src.cache_dir)


# --------------------------------------------------------------------------
# hips2fits
# --------------------------------------------------------------------------
def capture_hips_url(src: DssSource, monkeypatch, ctx=None) -> str:
    """Run one fetch with the network stubbed out, returning the URL requested."""
    seen: dict[str, str] = {}

    class FakeResp:
        def read(self):
            return survey_fits()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, *a, **k):
        seen["url"] = url
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    src.fetch(ctx or make_ctx())
    return seen["url"]


def test_hips_requests_fits_never_jpeg(monkeypatch):
    """The whole point of the back end: 16-bit survey values, not an 8-bit
    stretch someone else chose."""
    src = DssSource(survey="hips:CDS/P/DSS2/red")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(
        capture_hips_url(src, monkeypatch)).query)
    assert params["format"] == ["fits"]
    assert params["hips"] == ["CDS/P/DSS2/red"]
    assert params["projection"] == ["TAN"]
    assert params["width"] == params["height"], "cutout must be square"


def test_hips_request_is_north_up(monkeypatch):
    """No rotation_angle: baking the rotator angle into the request would make
    every rotator position its own download and its own cache entry."""
    src = DssSource(survey="hips:dss2r")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(
        capture_hips_url(src, monkeypatch)).query)
    assert "rotation_angle" not in params


def test_hips_short_name_is_resolved_in_the_request(monkeypatch):
    src = DssSource(survey="hips:ha")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(
        capture_hips_url(src, monkeypatch)).query)
    assert params["hips"] == ["simg.de/P/NSNS/DR0_2/halpha"]


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("dss", "CDS/P/DSS2/red"),
        ("DSS2R", "CDS/P/DSS2/red"),
        ("dss2-blue", "CDS/P/DSS2/blue"),
        ("Ha", "simg.de/P/NSNS/DR0_2/halpha"),
        ("OIII", "simg.de/P/NSNS/DR0_2/oiii"),
        ("ps1_r", "CDS/P/PanSTARRS/DR1/r"),
        # A full id is passed through untouched, so the whole CDS list works
        # without this table knowing about it.
        ("CDS/P/Finkbeiner/Halpha", "CDS/P/Finkbeiner/Halpha"),
    ],
)
def test_hips_id_resolution(given, expected):
    assert resolve_hips_id(given) == expected


def test_unknown_hips_short_name_is_rejected():
    with pytest.raises(SourceError, match="unknown HiPS short name"):
        resolve_hips_id("mysurvey")


def test_hips_aliases_are_all_monochrome():
    """A colour HiPS is 8-bit RGBA in a FITS wrapper, so none may be reachable
    by a short name - the shortcut would silently be the lossy path."""
    assert not [v for v in HIPS_ALIASES.values() if "color" in v.lower()]


def test_hips_error_body_is_surfaced(monkeypatch):
    """hips2fits answers an unknown id with 400 and a JSON description, which is
    the actual diagnosis - a bare "HTTP 400" is not."""
    import urllib.error

    def fake_urlopen(url, *a, **k):
        raise urllib.error.HTTPError(
            url, 400, "Bad Request", {},  # type: ignore[arg-type]
            io.BytesIO(
                b'{"title": "Unknown HiPS", '
                b'"description": "Could not find a HiPS matching \\"CDS/P/NOPE\\""}'
            ),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    src = DssSource(survey="hips:CDS/P/NOPE")
    with pytest.raises(SourceError, match="Could not find a HiPS"):
        src.fetch(make_ctx())


# --------------------------------------------------------------------------
# hips2fits host failover
# --------------------------------------------------------------------------
def fake_hosts(monkeypatch, behaviour: dict[str, object]) -> list[tuple[str, float]]:
    """Stub urlopen per hostname. Returns the (host, timeout) log of attempts.

    ``behaviour`` maps a hostname to what that host does: an exception instance
    to raise, or anything else to answer with a valid cutout.
    """
    calls: list[tuple[str, float]] = []

    class FakeResp:
        def read(self):
            return survey_fits()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, *a, timeout=None, **k):
        host = urllib.parse.urlparse(url).netloc
        calls.append((host, timeout))
        outcome = behaviour.get(host, "ok")
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


A, B = "a.example", "b.example"
TWO_HOSTS = (f"https://{A}/hips2fits", f"https://{B}/hips2fits")


def make_hips_source(**kw) -> DssSource:
    return DssSource(
        survey="hips:CDS/P/DSS2/red",
        hips=HipsEndpoint(TWO_HOSTS, probe_timeout_s=3.0),
        **kw,
    )


def test_default_hips_hosts_are_a_failover_chain():
    """Both CDS machines are listed: measured 2026-08-24, alasky accepted the
    connection and never answered while alaskybis served the same cutout in 10 s.
    One host means that outage is the whole survey path down."""
    assert len(HIPS_BASES) >= 2
    assert len({urllib.parse.urlparse(b).netloc for b in HIPS_BASES}) == len(HIPS_BASES)


def test_a_silent_host_fails_over_to_the_next(monkeypatch):
    src = make_hips_source()
    calls = fake_hosts(monkeypatch, {A: TimeoutError("The read operation timed out")})
    data, _ = src.fetch(make_ctx())
    assert [h for h, _ in calls] == [A, B]
    assert data.size > 0


def test_a_host_with_an_alternate_behind_it_gets_the_probe_timeout(monkeypatch):
    """A dead first host must not cost the full timeout_s per cutout - that is
    the 60 s stall this failover exists to remove. The last host still gets the
    full budget, because there is nothing left to fall back to."""
    src = make_hips_source(timeout_s=60.0)
    calls = fake_hosts(monkeypatch, {A: TimeoutError("timed out")})
    src.fetch(make_ctx())
    assert dict(calls) == {A: 3.0, B: 60.0}


def test_the_working_host_is_remembered(monkeypatch):
    """Rediscovering the dead host on every exposure would reintroduce the stall
    this removes, once per frame."""
    src = make_hips_source()
    calls = fake_hosts(monkeypatch, {A: TimeoutError("timed out")})
    src.fetch(make_ctx())
    src._mem.clear()
    src.fetch(make_ctx(ra=120.0, dec=10.0))
    assert [h for h, _ in calls] == [A, B, B], "the dead host was probed twice"


def test_the_pin_is_dropped_when_that_host_dies(monkeypatch):
    """Sticky must not mean stuck: the host that worked an hour ago is exactly
    the one that goes silent mid-session."""
    src = make_hips_source()
    fake_hosts(monkeypatch, {A: TimeoutError("timed out")})
    src.fetch(make_ctx())
    assert B in src.hips.pinned

    src._mem.clear()
    calls = fake_hosts(monkeypatch, {B: TimeoutError("timed out")})
    src.fetch(make_ctx(ra=200.0, dec=-5.0))
    assert [h for h, _ in calls] == [B, A]
    assert A in src.hips.pinned


def test_a_layer_shares_one_endpoint_with_every_other_layer(monkeypatch):
    """A DssSource per filter times out against the dead host once per filter
    unless they share the picker."""
    cfg = Config.model_validate(
        {
            "source": {
                "mode": "dss",
                "dss": {
                    "survey": "hips:dss2r",
                    "fallback_to_artificial": False,
                    "hips_bases": list(TWO_HOSTS),
                    "hips_probe_timeout_s": 3.0,
                    "per_filter": {
                        "Ha": {"survey": "hips:ha"},
                        "OIII": {"survey": "hips:oiii"},
                    },
                },
            },
            "filter_wheel": {"names": ["Ha", "OIII"], "focus_offsets": [120, 120]},
        }
    )
    src = build_source(cfg)
    endpoints = {id(src.default.hips), *(id(la.hips) for la in src.per_filter.values())}
    assert len(endpoints) == 1


def test_an_unknown_hips_id_is_not_retried_against_every_host(monkeypatch):
    """A 400 is the same 400 on every mirror. Failing over would burn a timeout
    per host and then report the last one's message instead of the real one."""
    src = make_hips_source()
    calls = fake_hosts(
        monkeypatch,
        {
            A: urllib.error.HTTPError(
                "u", 400, "Bad Request", {},  # type: ignore[arg-type]
                io.BytesIO(b'{"description": "Could not find a HiPS"}'),
            )
        },
    )
    with pytest.raises(SourceError, match="Could not find a HiPS"):
        src.fetch(make_ctx())
    assert [h for h, _ in calls] == [A], "a bad id was retried against the mirror"


def test_a_server_error_does_fail_over(monkeypatch):
    """5xx is the host's problem, unlike 4xx."""
    src = make_hips_source()
    calls = fake_hosts(
        monkeypatch,
        {A: urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b""))},  # type: ignore[arg-type]
    )
    src.fetch(make_ctx())
    assert [h for h, _ in calls] == [A, B]


def test_all_hosts_down_names_every_one_of_them(monkeypatch):
    """The whole point of the log line: which hosts were tried and what each
    said, so the next step is obvious."""
    src = make_hips_source()
    fake_hosts(
        monkeypatch,
        {A: TimeoutError("timed out"), B: OSError("connection refused")},
    )
    with pytest.raises(SourceError) as exc:
        src.fetch(make_ctx())
    msg = str(exc.value)
    assert A in msg and B in msg
    assert "timed out" in msg and "connection refused" in msg
    assert "CDS/P/DSS2/red" in msg, "the failing survey id is not in the error"


def test_an_empty_host_list_is_rejected():
    with pytest.raises(ValueError, match="no hips2fits host"):
        HipsEndpoint([])


def test_hips_reprojects_onto_the_sensor_grid(monkeypatch):
    src = DssSource(survey="hips:CDS/P/DSS2/red")
    monkeypatch.setattr(src, "_fetch_hips", lambda *a, **k: survey_fits())
    out = src.render(make_ctx())
    assert out.shape == SHAPE
    assert np.isfinite(out).all()
    assert out.max() > 0


def test_download_grid_tracks_the_sensor_plate_scale():
    """hips2fits resamples onto whatever grid it is asked for, so asking too
    small throws away detail the sensor could resolve."""
    src = DssSource(survey="hips:dss")
    ctx = make_ctx()  # 3"/px
    fine = DssSource(survey="hips:dss")._download_grid(0.5, make_ctx())
    assert src._download_grid(0.5, ctx) == fine
    # 0.5 deg at 3"/px is 600 px.
    assert src._download_grid(0.5, ctx) == 600
    # Clamped below, so a tiny guide subframe still gets something to reproject.
    assert src._download_grid(0.01, ctx) == 300
    # ...and above, out of politeness to a free service.
    assert DssSource(max_download_px=1000)._download_grid(10.0, ctx) == 1000


def test_download_grid_is_part_of_the_cache_key():
    src = DssSource(survey="hips:dss")
    coarse = src._cache_key(83.6, 22.0, 1.0, (100, 100), 300)
    fine = src._cache_key(83.6, 22.0, 1.0, (100, 100), 2000)
    assert coarse != fine


# --------------------------------------------------------------------------
# colour cubes and coverage
# --------------------------------------------------------------------------
def test_colour_hips_cube_is_rejected(monkeypatch):
    """``format=fits`` on a colour HiPS returns an (4, H, W) uint8 RGBA cube -
    the JPEG tiles in a FITS wrapper, bright cores already clipped. It used to
    reach ``reshape`` and die with a bare ValueError."""
    src = DssSource(survey="hips:CDS/P/DSS2/color")
    monkeypatch.setattr(src, "_fetch_hips", lambda *a, **k: survey_fits(planes=4))
    with pytest.raises(SourceError, match="colour HiPS"):
        src.fetch(make_ctx())


def test_degenerate_cube_axis_is_still_accepted(monkeypatch):
    """(1, H, W) is a wrapped 2-D plane, not a colour image - SkyView emits it."""
    src = DssSource(survey="hips:CDS/P/DSS2/red")
    monkeypatch.setattr(src, "_fetch_hips", lambda *a, **k: survey_fits(planes=1))
    assert src.render(make_ctx()).shape == SHAPE


def test_blank_survey_response_is_rejected(monkeypatch):
    src = DssSource(survey="hips:ps1r")
    monkeypatch.setattr(
        src, "_fetch_hips", lambda *a, **k: survey_fits(finite_frac=0.0)
    )
    with pytest.raises(SourceError, match="entirely blank"):
        src.fetch(make_ctx())


def test_frame_the_survey_barely_covers_is_rejected(monkeypatch):
    """PanSTARRS at M42 comes back 70% NaN because the saturated core is masked.
    Zero-filling that paints a black hole exactly where the target is, so we
    refuse the frame and let the fallback serve a usable one instead."""
    src = DssSource(survey="hips:ps1r", min_coverage=0.5)
    monkeypatch.setattr(
        src, "_fetch_hips", lambda *a, **k: survey_fits(finite_frac=0.2)
    )
    with pytest.raises(SourceError, match="covers only"):
        src.render(make_ctx())


def test_a_mostly_covered_frame_is_kept_and_gaps_are_empty_sky(monkeypatch):
    src = DssSource(survey="hips:dss", min_coverage=0.5)
    monkeypatch.setattr(
        src, "_fetch_hips", lambda *a, **k: survey_fits(finite_frac=0.95)
    )
    out = src.render(make_ctx())
    assert out.shape == SHAPE
    assert np.isfinite(out).all(), "gaps must be filled, not left as NaN"
    assert out.max() > 0


def test_coverage_failure_falls_back_to_the_artificial_sky(monkeypatch):
    """The guard is only useful if it degrades the run rather than ending it."""
    dss = DssSource(survey="hips:ps1r", min_coverage=0.5)
    monkeypatch.setattr(
        dss, "_fetch_hips", lambda *a, **k: survey_fits(finite_frac=0.2)
    )
    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    out = FallbackSource(dss, art).render(make_ctx())
    assert out.shape == SHAPE
    assert out.max() > 0


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("DSS2R", "DSS2 Red"),
        ("dss2r", "DSS2 Red"),
        ("DSS2 Red", "DSS2 Red"),
        ("dss2-blue", "DSS2 Blue"),
        ("DSS2IR", "DSS2 IR"),
        ("DSS", "DSS"),
        # Anything unknown goes to SkyView verbatim - it hosts hundreds.
        ("2MASS-K", "2MASS-K"),
    ],
)
def test_skyview_survey_aliases(given, expected):
    assert resolve_skyview_survey(given) == expected


def test_config_default_survey_is_a_resolvable_hips_id():
    from astroskysim.config import Config

    backend, _, survey = Config().source.dss.survey.partition(":")
    assert backend == "hips"
    # A full id, and a monochrome one - a colour HiPS would be rejected at decode.
    assert resolve_hips_id(survey) == survey
    assert "color" not in survey.lower()


def test_fallback_source_recovers_from_a_failing_primary():
    class Broken:
        name = "broken"

        def render(self, ctx):
            raise SourceError("network down")

    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    out = FallbackSource(Broken(), art).render(make_ctx())
    assert out.shape == SHAPE
    assert out.max() > 0, "fallback produced nothing"


# --------------------------------------------------------------------------
# composite
# --------------------------------------------------------------------------
def test_composite_includes_both_background_and_stars(monkeypatch):
    dss = DssSource(survey="eso:DSS")
    monkeypatch.setattr(dss, "_fetch_eso", lambda *a, **k: survey_fits())
    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)

    bg_only = dss.render(make_ctx())
    stars_only = art.render(make_ctx())
    both = CompositeSource(dss, art, suppress_background_stars=False).render(make_ctx())

    assert both.shape == SHAPE
    # The composite must exceed either component alone.
    assert both.sum() > bg_only.sum()
    assert both.sum() > stars_only.sum()
    assert both.sum() == pytest.approx(bg_only.sum() + stars_only.sum(), rel=1e-9)


def test_composite_weights_are_applied(monkeypatch):
    dss = DssSource(survey="eso:DSS")
    monkeypatch.setattr(dss, "_fetch_eso", lambda *a, **k: survey_fits())
    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)

    base = CompositeSource(dss, art, suppress_background_stars=False).render(make_ctx())
    no_bg = CompositeSource(
        dss, art, background_weight=0.0, suppress_background_stars=False
    ).render(make_ctx())
    no_stars = CompositeSource(
        dss, art, star_weight=0.0, suppress_background_stars=False
    ).render(make_ctx())

    assert no_bg.sum() < base.sum()
    assert no_stars.sum() < base.sum()
    assert no_bg.sum() == pytest.approx(art.render(make_ctx()).sum(), rel=1e-9)


def test_composite_survives_a_dead_background(monkeypatch):
    """A network failure must degrade to stars, not fail the exposure."""

    class Broken:
        name = "broken"

        def render(self, ctx):
            raise SourceError("network down")

    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    out = CompositeSource(Broken(), art).render(make_ctx())
    assert out.shape == SHAPE
    assert out.max() > 0


def test_suppress_point_sources_removes_stars_keeps_nebulosity():
    """A compact peak should go; a broad gradient should survive."""
    img = np.zeros((60, 60))
    yy, xx = np.mgrid[0:60, 0:60]
    nebula = 100.0 * np.exp(-(((xx - 30) ** 2 + (yy - 30) ** 2) / (2 * 15**2)))
    img += nebula
    img[10, 45] += 5000.0  # a star

    out = suppress_point_sources(img, scale_arcsec_px=1.0, seeing_arcsec=3.0)
    assert out[10, 45] < 500.0, "star was not suppressed"
    # The nebula centre should be largely intact.
    assert out[30, 30] > 0.7 * nebula[30, 30]


def test_composite_suppression_reduces_background_contribution(monkeypatch):
    dss = DssSource(survey="eso:DSS")
    monkeypatch.setattr(dss, "_fetch_eso", lambda *a, **k: survey_fits())
    art = ArtificialSource(SyntheticCatalog(seed=1), limiting_mag=14.0)
    on = CompositeSource(dss, art, suppress_background_stars=True).render(make_ctx())
    off = CompositeSource(dss, art, suppress_background_stars=False).render(make_ctx())
    assert on.sum() <= off.sum()


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (SourceMode.ARTIFICIAL, "artificial"),
        (SourceMode.DSS, "dss+fallback"),
        (SourceMode.COMPOSITE, "composite"),
    ],
)
def test_registry_builds_each_mode(mode, expected):
    cfg = Config()
    cfg.source.mode = mode
    assert build_source(cfg).name == expected


def test_registry_dss_without_fallback_is_bare():
    cfg = Config()
    cfg.source.mode = SourceMode.DSS
    cfg.source.dss.fallback_to_artificial = False
    assert build_source(cfg).name == "dss"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def test_calibrate_handles_all_nan():
    assert calibrate_survey_image(np.full((4, 4), np.nan)).max() == 0.0


def test_calibrate_handles_constant_image():
    """A flat frame carries no object signal at all - not a full-scale one.

    The percentile stretch this replaced mapped any gradient, however faint, to
    the full output range, so a blank field came back as bright as a nebula.
    """
    assert calibrate_survey_image(np.full((4, 4), 5.0)).max() == 0.0


def test_calibrate_is_never_negative_and_subtracts_the_survey_sky():
    """The survey's own sky maps to zero *added* signal, not to negative flux.

    The old stretch put the 5th percentile at zero, i.e. below the sky, so 5%
    of every frame was darker than an unilluminated detector can be.
    """
    out = calibrate_survey_image(np.arange(100.0).reshape(10, 10))
    assert out.min() == 0.0
    assert (out == 0.0).mean() >= 0.25, "the sky level and below must be flat zero"


def test_calibrate_reference_percentile_lands_on_one():
    rng = np.random.default_rng(3)
    data = 1000.0 + rng.normal(0, 5, (200, 200))
    data[90:110, 90:110] += 400.0  # 1% of the frame, brighter than sky
    out = calibrate_survey_image(data, ref_percentile=99.0)
    assert np.percentile(out, 99.0) == pytest.approx(1.0, rel=1e-6)


def test_calibrate_is_invariant_to_an_additive_pedestal():
    """Two surveys with different bias levels must give the same signal.

    The absolute pixel value of a survey cutout is an instrumental offset, not
    photometry, so it cannot be allowed to set the brightness of the frame.
    """
    rng = np.random.default_rng(11)
    data = 100.0 + rng.normal(0, 3, (120, 120))
    data[50:70, 50:70] += 200.0
    assert np.allclose(calibrate_survey_image(data), calibrate_survey_image(data + 5000.0))


def test_frame_wcs_tracks_subframe_and_binning():
    """A binned subframe's header must describe the pixels it actually holds.

    Building the header WCS from the delivered array dimensions instead puts the
    tangent point in the wrong place and reports the unbinned scale, which sends
    a plate solve looking at the wrong patch of sky.
    """
    from astroskysim.sky.wcs import frame_wcs, sensor_wcs

    full = sensor_wcs(80.0, 20.0, 1200, 1000, 1.5)
    start_x, start_y, bx, by = 200, 100, 2, 2
    sub = frame_wcs(full, start_x, start_y, bx, by, (450, 500))

    # Binned pixel (i, j) covers sensor pixels starting at start + i*bin, so its
    # centre is at start + i*bin + (bin-1)/2. Both WCS must agree on its sky
    # position.
    for i, j in ((0, 0), (137, 219), (449, 499)):
        want = full.all_pix2world(
            [[start_x + j * bx + (bx - 1) / 2.0, start_y + i * by + (by - 1) / 2.0]], 0
        )[0]
        got = sub.all_pix2world([[float(j), float(i)]], 0)[0]
        assert got[0] == pytest.approx(want[0], abs=1e-9)
        assert got[1] == pytest.approx(want[1], abs=1e-9)

    # The reported plate scale is the binned one.
    scale = np.hypot(*(sub.wcs.cd[:, 0])) * 3600.0
    assert scale == pytest.approx(1.5 * bx, rel=1e-9)
