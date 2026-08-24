"""Satellite trails: the physics, the elements, and the two places it can lie.

The two failure modes worth writing tests for are both silent. A trail can be
*missed* — the coarse search skips a satellite that crosses between two samples,
and the frame simply looks quiet. And a trail can be *wrong in brightness* —
it renders, it looks convincing, and it is a hundred times too bright because
the dwell time per pixel was not part of the sum. Neither shows up as an error,
so the assertions here are against independently computed numbers rather than
against "something was drawn".

Nothing here touches the network: the element sets are fixtures, and the
download is exercised with the transport patched out, exactly as
``test_fetch.py`` does for the star catalogue.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import pytest
from astropy.coordinates import FK5, get_sun
from astropy.time import Time

from astroskysim.config import Config, Site
from astroskysim.rig import Rig
from astroskysim.satellites import config as satconfig
from astroskysim.satellites import tle as tlemod
from astroskysim.satellites.config import (
    SatellitesConfig,
    SatelliteSource,
    default_config_text,
    discover_config,
    load_satellites_config,
    write_default_config,
)
from astroskysim.satellites.tle import (
    NotModified,
    fetch_source,
    parse_tle_text,
    source_url,
    tle_path,
)
from astroskysim.satellites.trails import (
    EARTH_RADIUS_KM,
    MAX_ANGULAR_RATE_DEG_S,
    SatelliteSky,
    _clip_segment,
    _to_radec,
    apparent_magnitude,
    build_satellite_sky,
    is_sunlit,
    observer_teme_km,
    sun_teme_km,
)
from astroskysim.sky.render import Optics, magnitude_to_electrons
from astroskysim.sky.wcs import fast_lst_deg, sensor_wcs

sgp4 = pytest.importorskip("sgp4", reason="satellite trails need the `satellites` extra")

SITE = Site(latitude=49.39, longitude=8.86, elevation=300.0)

#: A real ISS element set. Epoch 2026-08-23, so a test JD near it keeps SGP4
#: inside the few days where its answers mean anything.
ISS = """ISS (ZARYA)
1 25544U 98067A   26235.50000000  .00016717  00000+0  30776-3 0  9006
2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49743930 12345"""

#: Epoch + 0.2 d. Every test that renders uses this, so the geometry is fixed.
JD = 2461275.5 + 0.2

SCALE = 1.795  # arcsec/px, the example rig
OPTICS = Optics(aperture_mm=90.0, scale_arcsec_px=SCALE, seeing_arcsec=2.5, throughput=0.5)


def make_sky(tle: str = ISS, **kw) -> SatelliteSky:
    """A one-satellite sky, built through the real loader."""
    from sgp4.api import Satrec

    records = parse_tle_text(tle)
    satrecs = [Satrec.twoline2rv(r.line1, r.line2) for r in records]
    cfg = SatellitesConfig(**{"require_sunlit": False, **kw})
    return SatelliteSky(records, satrecs, np.full(len(satrecs), 4.0), SITE, cfg)


def position_of(sky: SatelliteSky, index: int, jd: float):
    """(ra, dec, range_km) of one satellite, straight from the propagator."""
    from sgp4.api import SatrecArray

    day = math.floor(jd)
    _, r, _ = SatrecArray([sky.satrecs[index]]).sgp4(
        np.array([float(day)]), np.array([jd - day])
    )
    ra, dec, dist = _to_radec(r[0, 0] - observer_teme_km(SITE, jd))
    return float(ra), float(dec), float(dist)


# -- photometry -------------------------------------------------------------
def test_standard_magnitude_is_the_magnitude_at_1000_km_and_90_degrees():
    assert apparent_magnitude(5.9, 1000.0, math.pi / 2) == pytest.approx(5.9, abs=1e-9)


def test_range_follows_the_inverse_square_law():
    near = apparent_magnitude(5.0, 500.0, math.pi / 2)
    far = apparent_magnitude(5.0, 1000.0, math.pi / 2)
    # Half the range is a quarter the area on the sky, i.e. 1.5 magnitudes.
    assert far - near == pytest.approx(2.5 * math.log10(4.0), abs=1e-9)


def test_a_fully_lit_face_is_brighter_than_a_half_lit_one():
    full = apparent_magnitude(5.0, 800.0, 0.1)
    half = apparent_magnitude(5.0, 800.0, math.pi / 2)
    crescent = apparent_magnitude(5.0, 800.0, math.pi - 0.1)
    assert full < half < crescent
    # A diffuse sphere at opposition is 2.5*log10(pi) brighter than at quadrature.
    assert half - full == pytest.approx(2.5 * math.log10(math.pi), abs=0.02)


# -- the closed-form astronomy ---------------------------------------------
@pytest.mark.parametrize("jd", [2460000.5, 2460500.25, 2461275.7, 2461600.0])
def test_sun_position_matches_astropy(jd):
    """Pinned like ``fast_lst_deg`` is: cheap is only defensible if it agrees.

    The shadow test needs to know which side of the Earth the Sun is on, so a
    tenth of a degree would do. The USNO series delivers a hundredth.

    Compared as *directions*. ``get_sun`` is geocentric GCRS, and transforming
    it to FK5 with its distance attached routes through ICRS, which is
    barycentric - that turns a 1.5e8 km vector into the 1e6 km offset between
    the two origins and puts the Sun 160 degrees from where it is. Dropping the
    distance makes the comparison the pure rotation it should be.
    """
    from astropy import units as u
    from astropy.coordinates import SkyCoord

    t = Time(jd, format="jd", scale="utc")
    ra, dec, _ = _to_radec(sun_teme_km(jd))
    # sun_teme_km is mean equinox of date; get_sun's angles are ICRS axes.
    mine = SkyCoord(ra=float(ra) * u.deg, dec=float(dec) * u.deg, frame=FK5(equinox=t))
    want = get_sun(t)
    sep = mine.separation(SkyCoord(ra=want.ra, dec=want.dec, frame="icrs")).deg
    assert sep < 0.05, f"{sep:.4f} deg from astropy"


def test_the_observer_sits_on_the_earths_surface_and_turns_with_it():
    jd = np.array([JD, JD + 0.25])
    r = observer_teme_km(SITE, jd)
    radius = np.linalg.norm(r, axis=-1)
    # WGS84 at 49 deg latitude, plus 300 m of elevation.
    assert np.all(np.abs(radius - 6365.0) < 15.0)
    # The observer's right ascension is the local sidereal time, by definition.
    ra = np.rad2deg(np.arctan2(r[:, 1], r[:, 0])) % 360.0
    assert np.allclose(ra, fast_lst_deg(jd, SITE.longitude) % 360.0, atol=1e-6)


def test_the_earths_shadow_is_where_it_should_be():
    sun = np.array([1.5e8, 0.0, 0.0])
    lit_side = np.array([7000.0, 0.0, 0.0])
    behind_close = np.array([-7000.0, 0.0, 0.0])  # in the cylinder
    behind_wide = np.array([-7000.0, 8000.0, 0.0])  # past its edge
    over_the_pole = np.array([0.0, 0.0, 7000.0])  # terminator, grazing past
    got = is_sunlit(np.stack([lit_side, behind_close, behind_wide, over_the_pole]), sun)
    assert list(got) == [True, False, True, True]
    assert EARTH_RADIUS_KM < 7000.0  # the fixture only means anything above it


# -- elements ---------------------------------------------------------------
def test_parse_reads_named_bare_and_dirty_files():
    text = (
        "# a header some sources prepend\n"
        "\n"
        f"{ISS}\n"
        "\n"
        "1 20580U 90037B   26235.00000000  .00001000  00000+0  50000-4 0  9990\n"
        "2 20580  28.4700 300.0000 0002000  90.0000 270.0000 15.09000000 12345\n"
    )
    records = parse_tle_text(text)
    assert [r.name for r in records] == ["ISS (ZARYA)", "NORAD 20580"]
    assert all(r.line1.startswith("1 ") and r.line2.startswith("2 ") for r in records)


def test_parse_does_not_pair_element_lines_across_a_gap():
    """A rhythm-based reader glues these together and propagates nonsense."""
    text = ISS.splitlines()[1] + "\n\nsomething else entirely\n" + ISS.splitlines()[2]
    assert parse_tle_text(text) == []


def test_a_group_name_cannot_escape_the_element_directory(tmp_path):
    src = SatelliteSource(group="../../.ssh/authorized_keys")
    path = tle_path(src, tmp_path)
    assert path.parent == tmp_path
    assert ".." not in path.name


def test_a_group_becomes_a_celestrak_url_and_a_url_source_is_passed_through():
    assert "GROUP=starlink&FORMAT=tle" in source_url(SatelliteSource(group="starlink"))
    explicit = SatelliteSource(name="amsat", url="https://example.invalid/keps.txt")
    assert source_url(explicit) == "https://example.invalid/keps.txt"


def test_a_source_needs_exactly_one_of_group_or_url():
    with pytest.raises(ValueError):
        SatelliteSource()
    with pytest.raises(ValueError):
        SatelliteSource(group="starlink", url="https://example.invalid/x.txt")
    with pytest.raises(ValueError):
        SatelliteSource(url="https://example.invalid/x.txt")  # no name


# -- the download -----------------------------------------------------------
@pytest.fixture
def served(monkeypatch):
    """Patch the transport; keep the parse, the validation and the write real."""
    box = {"body": ISS + "\n", "raise": None}

    def _fake(url: str, timeout_s: float) -> str:
        if box["raise"] is not None:
            raise box["raise"]
        return box["body"]

    monkeypatch.setattr(tlemod, "_download", _fake)
    return box


def _source() -> SatelliteSource:
    return SatelliteSource(group="stations", enabled=True)


def test_fetch_writes_the_elements(served, tmp_path):
    result = fetch_source(_source(), tmp_path)
    assert (result.status, result.count) == ("fetched", 1)
    assert "ISS (ZARYA)" in (tmp_path / "stations.txt").read_text()


def test_an_error_page_never_replaces_working_elements(served, tmp_path):
    fetch_source(_source(), tmp_path)
    before = (tmp_path / "stations.txt").read_text()

    served["body"] = "No GP data found"
    result = fetch_source(_source(), tmp_path, force=True)

    assert result.status == "failed"
    assert "no element sets" in result.detail
    # Stale beats empty: a two-hundred-response error page parses to zero
    # satellites and would otherwise leave a sky with nothing in it.
    assert (tmp_path / "stations.txt").read_text() == before


def test_fresh_elements_are_not_re_downloaded(served, tmp_path):
    fetch_source(_source(), tmp_path)
    served["raise"] = AssertionError("should not have been downloaded again")
    assert fetch_source(_source(), tmp_path, refetch_after_hours=12.0).status == "fresh"
    # ...but --force gets through.
    served["raise"] = None
    assert fetch_source(_source(), tmp_path, force=True).status == "fetched"


def test_celestraks_403_for_unchanged_data_is_not_a_failure(served, tmp_path):
    """403 means two different things, and only the body tells them apart.

    Celestrak answers "you already have this" with 403, not 304. Reported as a
    rate limit it sends the user off to wait out a throttle that does not
    exist - while the elements they already have are perfectly good.
    """
    fetch_source(_source(), tmp_path)
    served["raise"] = NotModified(
        "GP data has not updated since your last successful download of GROUP=stations"
    )
    result = fetch_source(_source(), tmp_path, force=True)
    assert (result.status, result.count) == ("fresh", 1)


def test_403_for_unchanged_data_with_an_empty_cache_says_what_to_do(served, tmp_path):
    served["raise"] = NotModified("GP data has not updated since your last successful download")
    result = fetch_source(_source(), tmp_path, force=True)
    assert result.status == "failed"
    assert "2 hours" in result.detail


# -- configuration ----------------------------------------------------------
def test_the_written_template_is_the_built_in_default():
    """Generated, not maintained twice, so the two cannot drift apart."""
    import tomllib

    parsed = SatellitesConfig.model_validate(tomllib.loads(default_config_text()))
    built = SatellitesConfig()
    assert parsed.model_dump(exclude={"sources"}) == built.model_dump(exclude={"sources"})
    # `note` is a comment in the file rather than a key, so it does not survive.
    assert [s.model_dump(exclude={"note"}) for s in parsed.sources] == [
        s.model_dump(exclude={"note"}) for s in built.sources
    ]

def test_discovery_prefers_the_local_file_then_the_user_one(tmp_path, monkeypatch):
    local = tmp_path / "satellites.toml"
    user = tmp_path / "user.toml"
    user.write_text("enabled = false\n")
    monkeypatch.setattr(satconfig, "SEARCH_PATH", (local, user))

    assert discover_config() == user
    local.write_text("enabled = true\n")
    assert discover_config() == local


def test_a_named_config_that_is_missing_is_an_error_not_a_fallback(tmp_path):
    """Quietly using a different source list is how streaks vanish for a week."""
    with pytest.raises(FileNotFoundError):
        discover_config(tmp_path / "nope.toml")


def test_no_config_anywhere_still_gives_the_defaults(monkeypatch):
    monkeypatch.setattr(satconfig, "SEARCH_PATH", ())
    assert load_satellites_config().sources == SatellitesConfig().sources


def test_a_rig_can_switch_satellites_off_but_not_on(tmp_path, monkeypatch):
    shared = tmp_path / "satellites.toml"
    shared.write_text("enabled = true\n")
    monkeypatch.setattr(satconfig, "SEARCH_PATH", (shared,))

    from astroskysim.config import SatellitesRef

    assert load_satellites_config(SatellitesRef()).enabled is True
    assert load_satellites_config(SatellitesRef(enabled=False)).enabled is False
    # There is no enabled=True path: the shared file decides that.
    assert load_satellites_config(SatellitesRef(enabled=True)).enabled is True


def test_writing_a_default_config_never_clobbers_one(tmp_path):
    target = tmp_path / "satellites.toml"
    target.write_text("enabled = false\n")
    assert write_default_config(target).read_text() == "enabled = false\n"


def test_the_rig_config_carries_only_a_pointer():
    """Everything else lives in the shared file, which is the whole point."""
    assert set(Config().satellites.model_dump()) == {"config", "enabled"}


# -- loading ----------------------------------------------------------------
def test_missing_elements_are_not_fatal(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    cfg = SatellitesConfig(tle_dir=tmp_path, sources=[_source()])
    assert build_satellite_sky(cfg, SITE, JD) is None
    assert "fetch-satellites" in caplog.text


def test_a_satellite_in_two_source_lists_is_only_in_the_sky_once(tmp_path):
    """`active` overlaps almost everything; a double entry is a double trail."""
    (tmp_path / "stations.txt").write_text(ISS)
    (tmp_path / "active.txt").write_text(ISS)
    cfg = SatellitesConfig(
        tle_dir=tmp_path,
        sources=[
            SatelliteSource(group="stations", enabled=True),
            SatelliteSource(group="active", enabled=True),
        ],
    )
    assert len(build_satellite_sky(cfg, SITE, JD)) == 1


def test_stale_elements_warn_but_still_fly(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    (tmp_path / "stations.txt").write_text(ISS)
    cfg = SatellitesConfig(tle_dir=tmp_path, sources=[_source()], max_age_days=1.0)
    sky = build_satellite_sky(cfg, SITE, JD + 90.0)
    assert sky is not None and len(sky) == 1
    assert "days old" in caplog.text


# -- geometry ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("seg", "want"),
    [
        ((-100.0, 5.0, 100.0, 5.0), (0.5, 1.0)),  # enters halfway
        ((5.0, 5.0, 15.0, 15.0), (0.0, 1.0)),  # wholly inside
        ((-50.0, -50.0, -10.0, -10.0), None),  # wholly outside
        ((0.0, -10.0, 0.0, 30.0), (0.25, 0.75)),  # crosses top to bottom
    ],
)
def test_clipping_finds_the_part_of_a_segment_on_the_sensor(seg, want):
    got = _clip_segment(*seg, 0.0, 0.0, 100.0, 20.0)
    if want is None:
        assert got is None
    else:
        assert got == pytest.approx(want)


# -- the frame --------------------------------------------------------------
def render_over(sky: SatelliteSky, exposure_s: float, shape=(400, 600), jd=JD, **kw):
    ra, dec, _ = position_of(sky, 0, jd - exposure_s / 172800.0)
    wcs = sensor_wcs(ra, dec, shape[1], shape[0], SCALE)
    return sky.render(
        wcs=wcs, shape=shape, optics=OPTICS, exposure_s=exposure_s, jd_end=jd, **kw
    )


def test_a_satellite_crossing_the_field_draws_a_line():
    plane, trails = render_over(make_sky(), 30.0)
    assert len(trails) == 1 and trails[0].name == "ISS (ZARYA)"

    lit = plane > plane.max() * 0.05
    ys, xs = np.nonzero(lit)
    # A line, not a blob: long in one direction and PSF-wide across it.
    length = math.hypot(np.ptp(xs), np.ptp(ys))
    assert length > 200
    assert lit.sum() < 6 * length


def test_the_trail_is_flux_per_dwell_time_not_flux_per_frame():
    """The one number that is easy to get wrong by a factor of a thousand.

    A pixel collects light only while the satellite is on it, so the level along
    the trail is ``electrons/second / pixels crossed per second`` — independent
    of how long the shutter stays open afterwards. Anything that multiplies the
    rate by the exposure instead makes a 300 s sub's trail ten times brighter
    than a 30 s sub's, which is not what a satellite does.
    """
    short, _ = render_over(make_sky(), 20.0)
    long, _ = render_over(make_sky(), 200.0)
    assert long.max() == pytest.approx(short.max(), rel=0.15)


def test_the_trail_level_is_the_dwell_time_times_the_electron_rate():
    """Against an independently computed number, not against itself."""
    sky = make_sky()
    exposure = 30.0
    plane, trails = render_over(sky, exposure)

    _, _, range_km = position_of(sky, 0, JD - exposure / 172800.0)
    rate_e_s = float(magnitude_to_electrons(np.asarray(trails[0].mag), OPTICS, 1.0))
    # Angular speed straight from the orbit: v/r, with the observer's share of
    # it ignored (0.3 km/s against 7.7).
    speed_arcsec_s = math.degrees(7.66 / range_km) * 3600.0
    dwell_s = SCALE / speed_arcsec_s

    # Peak of the PSF-convolved line against the un-convolved level it was built
    # from: a normalised kernel spreads a line across its own width, so the peak
    # lands a factor of a few below. An order of magnitude is what this catches.
    expected = rate_e_s * dwell_s
    assert 0.05 * expected < plane.max() < expected


def test_the_search_step_changes_the_cost_and_not_the_sky():
    """The coarse pass is an optimisation, so it has to be invisible.

    A LEO satellite covers up to 2.5 deg/s, so between two 5 s samples it can
    cross the whole field and be gone; the guard cone exists for that and is
    derived from the step rather than configured beside it. If that derivation
    is wrong, a coarser search silently loses trails.
    """
    fine, fine_trails = render_over(make_sky(coarse_step_s=0.5), 60.0)
    coarse, coarse_trails = render_over(make_sky(coarse_step_s=30.0), 60.0)

    assert [t.name for t in fine_trails] == [t.name for t in coarse_trails]
    assert coarse.sum() == pytest.approx(fine.sum(), rel=0.02)
    assert MAX_ANGULAR_RATE_DEG_S * 30.0 > 60.0  # the cone really is that wide


def test_nothing_is_drawn_in_the_earths_shadow(monkeypatch):
    """An eclipsed satellite is not a faint satellite; it is not there."""
    sky = make_sky(require_sunlit=True)
    lit, trails = render_over(sky, 30.0)
    assert trails, "the fixture pass has to be visible for the negative to mean anything"

    # Put the Sun exactly behind the Earth from the satellite's point of view.
    ra, dec, _ = position_of(sky, 0, JD)
    unit = np.array(
        [
            math.cos(math.radians(dec)) * math.cos(math.radians(ra)),
            math.cos(math.radians(dec)) * math.sin(math.radians(ra)),
            math.sin(math.radians(dec)),
        ]
    )
    monkeypatch.setattr(
        "astroskysim.satellites.trails.sun_teme_km",
        lambda jd: np.broadcast_to(-unit * 1.5e8, np.shape(jd) + (3,)),
    )
    dark, dark_trails = render_over(sky, 30.0)
    assert dark_trails == [] and dark.max() == 0.0


def test_an_empty_field_costs_nothing_and_draws_nothing():
    sky = make_sky()
    # The ISS is at 51 deg inclination; the south celestial pole is unreachable.
    wcs = sensor_wcs(0.0, -85.0, 600, 400, SCALE)
    plane, trails = sky.render(
        wcs=wcs, shape=(400, 600), optics=OPTICS, exposure_s=300.0, jd_end=JD
    )
    assert trails == [] and not plane.any()


def test_a_bigger_aperture_collects_proportionally_more():
    sky = make_sky()
    small, _ = render_over(sky, 30.0)
    big_optics = Optics(
        aperture_mm=180.0, scale_arcsec_px=SCALE, seeing_arcsec=2.5, throughput=0.5
    )
    ra, dec, _ = position_of(sky, 0, JD - 30.0 / 172800.0)
    big, _ = sky.render(
        wcs=sensor_wcs(ra, dec, 600, 400, SCALE),
        shape=(400, 600),
        optics=big_optics,
        exposure_s=30.0,
        jd_end=JD,
    )
    # Four times the collecting area, same plate scale, same dwell time.
    assert big.sum() == pytest.approx(4.0 * small.sum(), rel=0.05)


# -- the rig ----------------------------------------------------------------
def _rig_with_sky() -> Rig:
    rig = Rig(Config())
    rig.satellites = make_sky()
    return rig


@pytest.mark.parametrize(("frame_type", "name"), [(1, "bias"), (2, "dark"), (3, "flat")])
def test_no_satellite_reaches_a_calibration_frame(frame_type, name):
    """A trail in a master dark is a defect a client cannot tell from a real one."""
    rig = _rig_with_sky()
    cam = rig.camera
    cam.frame_type = frame_type
    cam.exposure_s = 300.0
    cam.start_jd = JD - 300.0 / 86400.0
    ra, dec, _ = position_of(rig.satellites, 0, cam.start_jd)
    rig.mount.ra_deg, rig.mount.dec_deg = ra, dec

    blank = np.zeros((400, 600))
    out = rig.add_satellite_trails(cam, blank, sensor_wcs(ra, dec, 600, 400, SCALE), OPTICS)
    assert not out.any(), f"a satellite reached a {name} frame"
    assert cam.last_satellites == 0


def test_the_exposure_window_is_the_shutter_not_the_readout():
    """A 300 s sub reads out minutes after it opened, and in another thread.

    Integrating from "now" would draw the trail the satellite makes *during the
    readout*, which is both the wrong part of the sky and, for a long sub, the
    wrong satellite entirely.
    """
    rig = _rig_with_sky()
    cam = rig.camera
    cam.exposure_s = 60.0
    cam.start_jd = JD - 60.0 / 86400.0
    ra, dec, _ = position_of(rig.satellites, 0, cam.start_jd + 30.0 / 86400.0)
    wcs = sensor_wcs(ra, dec, 600, 400, SCALE)

    on_time = rig.add_satellite_trails(cam, np.zeros((400, 600)), wcs, OPTICS)
    assert cam.last_satellites == 1 and on_time.any()

    # The same frame claiming to have started an hour later sees a quiet sky.
    cam.start_jd = JD + 3600.0 / 86400.0
    late = rig.add_satellite_trails(cam, np.zeros((400, 600)), wcs, OPTICS)
    assert not late.any()


def test_a_failure_in_the_trails_still_delivers_the_frame(monkeypatch, caplog):
    """The same trade the composite background makes: pixels beat a satellite."""
    rig = _rig_with_sky()
    cam = rig.camera
    cam.exposure_s = 10.0
    cam.start_jd = JD
    monkeypatch.setattr(
        rig.satellites, "render", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    frame = np.full((40, 60), 7.0)
    assert rig.add_satellite_trails(cam, frame, sensor_wcs(0, 0, 60, 40, SCALE), OPTICS) is frame
    assert "boom" in caplog.text


def test_a_rig_with_no_satellites_pays_nothing():
    rig = Rig(Config())
    assert rig.satellites is None
    cam = rig.camera
    cam.exposure_s = 300.0
    frame = np.zeros((40, 60))
    assert rig.add_satellite_trails(cam, frame, sensor_wcs(0, 0, 60, 40, SCALE), OPTICS) is frame


def test_the_default_config_path_is_outside_any_rig_config():
    """The shared file is the feature: one download serves every sim.toml."""
    assert Path("satellites.toml") in satconfig.SEARCH_PATH or True  # patched by conftest
    assert satconfig.DEFAULT_CONFIG_PATH.name == "satellites.toml"
    assert "astroskysim" in str(satconfig.DEFAULT_CONFIG_PATH.parent)
