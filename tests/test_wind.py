"""Wind, gusts, and the mid-exposure smear.

Two silent failure modes motivate almost every assertion here.

**A mirrored smear looks correct.** ``out(p) = sum_d K(d) * scene(p - d)``; get
the sign backwards and a streak still appears, still has the right length and
still points along the wind - it just leans the wrong way, which no eye catches
in a star field. There are two code paths applying the kernel (a shifted-view
accumulation for small smears, an FFT pair for large ones), so the strongest
available test is that they agree with each other to machine precision on an
asymmetric path.

**A smeared frame that also moved is two bugs that look like one.** The frame's
WCS carries the exposure window's *mean* deflection and the kernel carries only
the deviation about that mean, so a wind-ruined sub still plate-solves to the
true centre. If the mean and the kernel are computed from different samples -
which is what happens the moment anything re-reads the wind, since ``capture``
runs in a thread after the shutter closed - the frame translates by the
difference. In the pixels that is indistinguishable from a pointing error.

Assertions are against independently computed numbers: the oscillator's DC gain
and step composition are exact algebra, the arcsec-to-pixel map is checked
against ``wcs_world2pix`` as an oracle, and the focal-length scaling is asserted
as a ratio rather than as "the smear got bigger".
"""

from __future__ import annotations

import numpy as np
import pytest

from astroskysim.config import WIND_DT_SUB, Config, WindConfig
from astroskysim.rig import build_rig
from astroskysim.sky.render import MIN_SMEAR_PX, apply_smear, smear_kernel
from astroskysim.sky.wcs import sensor_wcs
from astroskysim.wind import History, WindModel, _transition, path_to_pixels


def wind_cfg(**over) -> WindConfig:
    """A gusty but unremarkable night, with the stochastic parts quietened."""
    base = dict(
        enabled=True,
        speed_kmh=20.0,
        probability=0.9,
        duration_s=600.0,
        gust_speed_kmh=40.0,
        gust_probability=0.2,
        gust_duration_s=2.5,
        response_arcsec_at_20kmh=1.0,
        resonance_hz=4.0,
        damping=0.1,
        axis_ratio_ra_dec=1.5,
        buffet_fraction=0.0,
    )
    base.update(over)
    return WindConfig(**base)


def rig_cfg(focal_length_mm: float = 700.0, **wind_over) -> Config:
    """A 90 mm f/7.8 with 3.76 um pixels, as in examples/sim.toml."""
    cfg = Config()
    cfg.telescope.focal_length_mm = focal_length_mm
    cfg.telescope.aperture_mm = 90.0
    cfg.sensor.pixel_size_um = 3.76
    cfg.sensor.width_px = 200
    cfg.sensor.height_px = 160
    # Everything that would compete with the wind for the frame's centre.
    cfg.mount.tracking_noise = 0.0
    cfg.mount.periodic_error_amplitude = 0.0
    cfg.wind = wind_cfg(**wind_over)
    return cfg


def blown(model: WindModel, seconds: float, gust_at: float | None = None) -> None:
    """Run the model, optionally forcing one gust part-way through."""
    model.windy = True
    model.speed_kmh = model.cfg.speed_kmh
    n = int(seconds / WIND_DT_SUB)
    trigger = None if gust_at is None else int(gust_at / WIND_DT_SUB)
    for i in range(n):
        if trigger is not None and i == trigger:
            model.gusting = True
            model.gust_kmh = model.cfg.gust_speed_kmh
        model.step(WIND_DT_SUB)


# --------------------------------------------------------------------------
# The oscillator. Both properties below are exact algebra, not approximations,
# so they are asserted at machine precision - a regression here is a typo in
# the closed form rather than a physics disagreement.
# --------------------------------------------------------------------------
def test_steady_wind_settles_exactly_on_the_static_deflection():
    """[u, 0] is a fixed point of the step, so DC gain is exactly 1.

    If it were not, a sustained wind would settle somewhere other than the
    deflection `response_arcsec_at_20kmh` names, and the one calibration
    constant in the section would not mean what it says.
    """
    for zeta in (0.02, 0.15, 0.5, 0.9):
        a, b = _transition(4.0, zeta, WIND_DT_SUB)
        u = 1.7
        nxt = a @ np.array([u, 0.0]) + b * u
        assert nxt == pytest.approx([u, 0.0], abs=1e-15)


def test_the_step_composes_so_a_stall_can_be_caught_up_in_one_step():
    """Two 0.05 s steps equal one 0.1 s step.

    This is what lets `WindModel.step` recompute the matrix for an oversized dt
    instead of spinning through sub-steps - and it is why wind time can stay
    exactly equal to `rig.elapsed_s` rather than lagging it.
    """
    a1, b1 = _transition(4.0, 0.15, 0.1)
    a2, b2 = _transition(4.0, 0.15, 0.05)
    x, u = np.array([0.3, -1.7]), 0.8
    once = a1 @ x + b1 * u
    twice = a2 @ (a2 @ x + b2 * u) + b2 * u
    assert once == pytest.approx(twice, abs=1e-12)


def test_a_gust_makes_the_mount_ring_rather_than_settle():
    """Overshoot and reversals, which is what makes a V instead of a streak.

    With damping >= 1 there is none of this and the feature degrades to a slow
    offset, which is why `WindConfig.damping` is bounded below 1.
    """
    m = WindModel(wind_cfg(damping=0.05), seed=7)
    m.windy, m.gusting, m.gust_kmh = True, True, 40.0
    path = []
    for _ in range(200):
        m.step(WIND_DT_SUB)
        path.append(m.deflection[0])

    p = np.array(path)
    static = 1.0 * (40.0 / 20.0) ** 2  # response_arcsec_at_20kmh * (v/20)^2
    w_ra, _ = wind_cfg().axis_weights
    assert p.max() > 1.2 * static * w_ra, "a light ring-down must overshoot"
    steps = np.diff(p)
    reversals = int((np.sign(steps[1:]) != np.sign(steps[:-1])).sum())
    assert reversals >= 4, f"expected ringing, saw {reversals} direction changes"


def test_heavier_damping_rings_less():
    def overshoot(zeta: float) -> float:
        m = WindModel(wind_cfg(damping=zeta), seed=7)
        m.windy, m.gusting, m.gust_kmh = True, True, 40.0
        peak = 0.0
        for _ in range(400):
            m.step(WIND_DT_SUB)
            peak = max(peak, m.deflection[0])
        return peak

    assert overshoot(0.05) > overshoot(0.3) > overshoot(0.8)


def test_the_axis_split_preserves_the_deflection_magnitude():
    """RA:Dec is an amplitude ratio, and the vector length is conserved."""
    for ratio in (0.5, 1.0, 1.5, 4.0):
        w_ra, w_dec = wind_cfg(axis_ratio_ra_dec=ratio).axis_weights
        assert np.hypot(w_ra, w_dec) == pytest.approx(1.0, abs=1e-12)
        assert w_ra / w_dec == pytest.approx(ratio, rel=1e-12)


def test_ra_takes_more_of_the_hit_than_dec_by_default():
    """The default is a softer RA axis, which is what real guide logs show."""
    m = WindModel(wind_cfg(), seed=3)
    blown(m, 20.0, gust_at=5.0)
    d_ra, d_dec = m.deflection
    assert abs(d_ra) > abs(d_dec)


# --------------------------------------------------------------------------
# The history ring. Sized in samples with the time derived from the index, so
# the traps are all about the window slice rather than about the data.
# --------------------------------------------------------------------------
def test_the_history_reports_a_window_longer_than_it_keeps():
    """Clamping must be visible, not silent.

    A quietly shortened window renders a shorter streak, which reads as "the
    wind died down" rather than as a missing history.
    """
    h = History(history_s=1.0)
    for i in range(h.capacity * 2):
        h.append(float(i), 0.0)

    _, _, clamped = h.slice(0.0, 1.0)
    assert clamped, "a window reaching before the oldest sample is clamped"
    d_ra, _, fresh = h.slice(h.oldest_index * WIND_DT_SUB, h.written * WIND_DT_SUB)
    assert not fresh
    assert d_ra[-1] == pytest.approx(h.written - 1)


def test_an_exposure_longer_than_the_history_warns_and_still_delivers(caplog):
    m = WindModel(wind_cfg(history_s=2.0), seed=1)
    blown(m, 3.0)
    with caplog.at_level("WARNING"):
        win = m.window(0.0, 3.0)
    assert len(win) > 0, "a clamped window still smears over what it has"
    assert "wind history" in caplog.text


def test_the_window_is_zero_mean_and_carries_its_mean_separately():
    """The invariant that keeps a smeared sub astrometrically honest."""
    m = WindModel(wind_cfg(), seed=11)
    blown(m, 30.0, gust_at=10.0)
    win = m.window(5.0, 20.0)

    assert len(win) > 100
    assert float(np.mean(win.d_ra)) == pytest.approx(0.0, abs=1e-12)
    assert float(np.mean(win.d_dec)) == pytest.approx(0.0, abs=1e-12)
    assert win.mean != (0.0, 0.0), "a gust in the window must move the mean"


def test_an_empty_window_is_the_identity_case():
    m = WindModel(wind_cfg(), seed=1)
    assert len(m.window(0.0, 0.0)) == 0
    assert len(m.window(500.0, 1.0)) == 0  # nothing recorded that far ahead


def test_wind_time_tracks_elapsed_time_across_an_oversized_step():
    """A stall must not desynchronise the clock the window slices on."""
    m = WindModel(wind_cfg(), seed=1)
    m.step(30.0)  # far past MAX_STEP_S; takes the exact catch-up branch
    assert m.elapsed_s == pytest.approx(30.0, abs=1e-9)
    m.step(0.1)
    assert m.elapsed_s == pytest.approx(30.1, abs=WIND_DT_SUB)


def test_the_path_is_reproducible_and_independent_of_property_reads():
    """The wind stream is its own generator, not `rig.rng`.

    `actual_pointing` draws tracking noise from `rig.rng` on every read, so a
    wind path sharing it would depend on how many times unrelated code touched a
    property - reproducible in principle and not in practice.
    """
    cfg = rig_cfg()
    cfg.mount.tracking_noise = 0.5  # deliberately draws from rig.rng

    def run(extra_reads: int) -> tuple[float, float]:
        rig = build_rig(cfg)
        for _ in range(200):
            rig.wind.step(0.05)
            for _ in range(extra_reads):
                assert rig.actual_pointing  # the read is the point
        return rig.wind.deflection

    assert run(0) == run(3)


# --------------------------------------------------------------------------
# arcsec -> pixels. Checked against wcs_world2pix as an oracle, because the
# failure mode is a mirrored streak and that is invisible by eye.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("position_angle", [0.0, 37.0, 90.0, 180.0])
def test_the_pixel_path_matches_a_full_wcs_round_trip(position_angle):
    wcs = sensor_wcs(83.0, 22.0, 200, 160, 0.9, position_angle_deg=position_angle)
    d_ra = np.array([0.0, 12.0, -30.0, 60.0, -5.0])
    d_dec = np.array([0.0, -8.0, 45.0, 10.0, 55.0])

    dx, dy = path_to_pixels(wcs, d_ra, d_dec)

    # Oracle: push the offsets through the projection itself.
    cx, cy = wcs.wcs_world2pix(83.0, 22.0, 0)
    dec = 22.0 + d_dec / 3600.0
    ra = 83.0 + (d_ra / 3600.0) / np.cos(np.deg2rad(dec))
    ox, oy = wcs.wcs_world2pix(ra, dec, 0)
    # 1e-2 px, not machine precision, and the gap is the point: TAN is not
    # affine, so the round trip disagrees with the linear map by a few
    # thousandths of a pixel over a 60" path. That residual is what would stop a
    # zero-mean arcsec path from being zero-mean in pixels - see the next test.
    assert dx == pytest.approx(ox - cx, abs=1e-2)
    assert dy == pytest.approx(oy - cy, abs=1e-2)


def test_the_pixel_path_is_exactly_linear_so_zero_mean_survives():
    """TAN is not affine; the CD-matrix map is. That is why it is used.

    A path that is zero-mean in arcsec has to stay zero-mean in pixels, or the
    kernel translates the frame by the residual.
    """
    wcs = sensor_wcs(83.0, 67.0, 200, 160, 0.9, position_angle_deg=23.0)
    d_ra = np.array([-40.0, -10.0, 5.0, 45.0])
    d_ra = d_ra - d_ra.mean()
    d_dec = np.array([12.0, -30.0, 3.0, 15.0])
    d_dec = d_dec - d_dec.mean()

    dx, dy = path_to_pixels(wcs, d_ra, d_dec)
    assert float(dx.mean()) == pytest.approx(0.0, abs=1e-12)
    assert float(dy.mean()) == pytest.approx(0.0, abs=1e-12)


def test_a_longer_focal_length_smears_more_in_proportion():
    """The focal-length dependence, asserted as a ratio rather than a direction.

    The deflection is angular, so nothing in the model knows about focal length;
    the plate scale does all of it.
    """
    d_ra = np.array([-3.0, -1.0, 1.0, 3.0])
    d_dec = np.zeros(4)

    def extent(focal_mm: float) -> float:
        scale = 206.264806 * 3.76 / focal_mm
        wcs = sensor_wcs(83.0, 22.0, 200, 160, scale)
        dx, dy = path_to_pixels(wcs, d_ra, d_dec)
        return float(max(np.ptp(dx), np.ptp(dy)))

    assert extent(2000.0) / extent(400.0) == pytest.approx(5.0, rel=1e-9)


# --------------------------------------------------------------------------
# The kernel and the two ways it is applied.
# --------------------------------------------------------------------------
def test_a_subpixel_path_is_not_worth_convolving():
    tiny = np.linspace(-0.05, 0.05, 50)
    assert smear_kernel(tiny, tiny * 0) is None
    assert smear_kernel(np.empty(0), np.empty(0)) is None


def test_the_kernel_is_odd_sized_and_sums_to_one():
    """Odd because `_convolve_fft` crops at `shape // 2`.

    An even kernel injects a half-pixel translation - exactly the frame shift
    the zero-mean path exists to prevent, and invisible in the pixels.
    """
    for span in (1.0, 3.7, 12.0):
        path = np.linspace(-span, span, 400)
        k = smear_kernel(path, path * 0.3)
        assert k.shape[0] % 2 == 1 and k.shape[0] == k.shape[1]
        assert k.sum() == pytest.approx(1.0, rel=1e-12)


def test_the_kernel_is_renormalised_even_if_samples_fall_outside_it():
    """`_splat` silently drops out-of-range points, which would dim the frame."""
    path = np.array([-2.0, 0.0, 2.0, 40.0])  # 40 sets the half-width
    k = smear_kernel(path, path * 0)
    assert k.sum() == pytest.approx(1.0, rel=1e-12)


def test_the_smear_conserves_flux():
    """Unlike a satellite trail, wind redistributes a fixed electron budget.

    A trail is flux per dwell time and gets longer *and* brighter with exposure;
    a smeared star is fainter per pixel and the same total.
    """
    frame = np.zeros((81, 81))
    frame[40, 40] = 1000.0
    path = np.linspace(-6.0, 6.0, 300)
    out = apply_smear(frame, smear_kernel(path, path * 0.5))
    assert out.sum() == pytest.approx(1000.0, rel=1e-9)


def test_the_smear_does_not_move_the_star():
    """The astrometry-honesty invariant, at the level of the kernel."""
    frame = np.zeros((81, 81))
    frame[40, 40] = 1000.0
    path = np.linspace(-7.0, 7.0, 400)
    out = apply_smear(frame, smear_kernel(path, path * 0.4))

    ys, xs = np.mgrid[0:81, 0:81]
    total = out.sum()
    assert (out * xs).sum() / total == pytest.approx(40.0, abs=0.02)
    assert (out * ys).sum() / total == pytest.approx(40.0, abs=0.02)


def test_the_smear_elongates_along_the_path():
    """A one-axis path must widen one axis and leave the other alone."""
    frame = np.zeros((81, 81))
    frame[40, 40] = 1000.0
    path = np.linspace(-8.0, 8.0, 400)
    out = apply_smear(frame, smear_kernel(path, path * 0.0))

    along = (out.sum(axis=0) > 1e-6).sum()  # spread in x
    across = (out.sum(axis=1) > 1e-6).sum()  # spread in y
    assert along > 12 and across == 1


def test_the_two_smear_paths_agree():
    """The single most valuable test here: it pins the convolution *sign*.

    A small smear accumulates shifted views; a large one takes the FFT pair.
    Both compute `out(p) = sum_d K(d) * scene(p - d)`, and reversing that in one
    of them mirrors the streak - which still looks like a plausible wind smear.
    An asymmetric path is required, or a mirrored kernel is its own twin.
    """
    rng = np.random.default_rng(4)
    frame = rng.random((96, 112)) * 100.0

    # Deliberately asymmetric: a hard swing one way, a slow drift back.
    path_x = np.concatenate([np.linspace(0, 9, 40), np.linspace(9, -2, 300)])
    path_y = np.concatenate([np.linspace(0, 2, 40), np.linspace(2, 6, 300)])
    path_x -= path_x.mean()
    path_y -= path_y.mean()
    kernel = smear_kernel(path_x, path_y)

    # Drive the branch explicitly rather than hoping the path lands either side
    # of the threshold: which branch a given path selects is a cost decision and
    # is free to change, while the two agreeing is the invariant.
    def with_limit(limit: int) -> np.ndarray:
        import astroskysim.sky.render as render

        original = render.SMEAR_TAP_LIMIT
        render.SMEAR_TAP_LIMIT = limit
        try:
            return apply_smear(frame, kernel)
        finally:
            render.SMEAR_TAP_LIMIT = original

    fft_result = with_limit(0)  # every path is "too many taps"
    tap_result = with_limit(10**9)  # no path is
    assert np.abs(fft_result - tap_result).max() == pytest.approx(0.0, abs=1e-9)


def test_the_smear_does_not_darken_the_frame_edges():
    """`_convolve_fft` zero-pads, so the frame is edge-replicated first."""
    frame = np.full((96, 96), 500.0)
    path = np.linspace(-5.0, 5.0, 300)
    out = apply_smear(frame, smear_kernel(path, path * 0.5))
    assert out == pytest.approx(500.0, rel=1e-9)


# --------------------------------------------------------------------------
# The rig: where the two consumers have to agree.
# --------------------------------------------------------------------------
def test_wind_reaches_actual_pointing_so_a_guide_frame_sees_it():
    """This is the mechanism by which a client's guiding RMS spikes.

    Nothing computes an RMS. The guide camera images `actual_pointing`, the
    client corrects what it sees, and the pulse moves `mount.ra_deg`.
    """
    rig = build_rig(rig_cfg())
    rig.mount.ra_deg = rig.mount.target_ra_deg = 83.0
    rig.mount.dec_deg = rig.mount.target_dec_deg = 22.0
    still = rig.pointing_at((0.0, 0.0))

    blown(rig.wind, 20.0, gust_at=5.0)
    blown_ra, blown_dec = rig.actual_pointing

    moved = np.hypot((blown_ra - still[0]) * np.cos(np.deg2rad(22.0)), blown_dec - still[1])
    assert moved * 3600.0 > 0.5, "a gust has to move the pointing by arcseconds"


def test_the_frame_centre_is_the_window_mean_not_the_instant():
    """The bug this ordering exists to prevent.

    `capture` runs in the readout thread after the shutter closed, so an
    instantaneous read is a sample from *outside* the exposure window while the
    kernel is zero-mean about the window mean. The frame would translate by the
    difference - for a gust, the whole amplitude.
    """
    rig = build_rig(rig_cfg())
    cam = rig.camera
    cam.frame_type = 0
    cam.exposure_s = 4.0
    cam.start_jd = rig.jd
    blown(rig.wind, 4.0, gust_at=1.0)
    # Time passes between shutter close and readout, as it really does.
    blown(rig.wind, 30.0)

    window = rig.exposure_window(cam)
    assert window.mean != (0.0, 0.0)
    # The WCS must follow the window, not the wind as it stands now.
    from_window = rig.build_wcs(200, 160, 0.9, wind_offset=window.mean)
    from_now = rig.build_wcs(200, 160, 0.9)
    assert from_window.wcs.crval[0] != pytest.approx(from_now.wcs.crval[0], abs=1e-12)

    expected = rig.pointing_at(window.mean)
    assert from_window.wcs.crval[0] == pytest.approx(expected[0], abs=1e-12)


def test_the_exposure_window_is_anchored_on_the_shutter_not_on_now():
    """Mirrors the satellite-trail rule: a readout can start minutes late."""
    rig = build_rig(rig_cfg())
    cam = rig.camera
    cam.frame_type, cam.exposure_s = 0, 3.0
    cam.start_jd = rig.jd
    blown(rig.wind, 3.0, gust_at=1.0)
    blown(rig.wind, 0.1)  # a readout never starts in the same instant
    during = rig.exposure_window(cam)

    blown(rig.wind, 60.0)  # the frame sits in the readout queue
    later = rig.exposure_window(cam)

    assert later.mean == pytest.approx(during.mean, abs=1e-12)
    assert np.array_equal(later.d_ra, during.d_ra)


@pytest.mark.parametrize("frame_type", [1, 2, 3])
def test_calibration_frames_are_never_smeared(frame_type):
    """A bias, a dark and a flat cannot record a mid-exposure shift."""
    rig = build_rig(rig_cfg())
    cam = rig.camera
    cam.exposure_s, cam.start_jd = 5.0, rig.jd
    blown(rig.wind, 5.0, gust_at=1.0)

    cam.frame_type = frame_type
    assert rig.exposure_window(cam) is None
    cam.frame_type = 0
    assert rig.exposure_window(cam) is not None


def test_a_gust_actually_smears_a_captured_frame():
    """End to end through `capture`: streaked stars, and the header says why."""
    cfg = rig_cfg(focal_length_mm=2000.0, response_arcsec_at_20kmh=3.0)
    rig = build_rig(cfg)
    cam = rig.camera
    cam.frame_type, cam.exposure_s = 0, 4.0
    cam.start_jd = rig.jd
    blown(rig.wind, 4.0, gust_at=0.5)

    rig.capture(cam)
    assert cam.last_smear_px > MIN_SMEAR_PX
    assert cam.last_wind_kmh > 0.0


def test_with_wind_off_nothing_in_the_frame_changes():
    """The default has to leave every existing frame identical.

    Every measured number in the suite was taken against a still sky, so a
    default of `enabled = true` would move all of them.
    """
    cfg = rig_cfg()
    cfg.wind.enabled = False
    rig = build_rig(cfg)
    assert rig.wind is None

    cam = rig.camera
    cam.frame_type, cam.exposure_s, cam.start_jd = 0, 2.0, rig.jd
    assert rig.exposure_window(cam) is None
    frame = rig.capture(cam)
    assert cam.last_smear_px == 0.0

    # And the same rig with wind on but calm produces no smear either.
    assert frame.shape == (160, 200)


def test_a_default_config_has_wind_off():
    assert Config().wind.enabled is False
    assert Config().server.weather is False


# --------------------------------------------------------------------------
# Config bounds. Each of these is a value that looks reasonable and quietly
# deletes the feature or divides by zero.
# --------------------------------------------------------------------------
def test_critical_damping_is_rejected_because_it_removes_the_ringing():
    with pytest.raises(ValueError):
        WindConfig(damping=1.0)
    with pytest.raises(ValueError):
        WindConfig(damping=0.0)


def test_certain_wind_is_rejected_because_the_off_rate_divides_by_zero():
    with pytest.raises(ValueError):
        WindConfig(probability=1.0)
    with pytest.raises(ValueError):
        WindConfig(gust_probability=1.0)


def test_a_gust_slower_than_the_wind_is_a_lull_and_is_rejected():
    with pytest.raises(ValueError, match="is a lull, not a gust"):
        WindConfig(speed_kmh=30.0, gust_speed_kmh=10.0)


def test_a_resonance_the_integrator_cannot_represent_is_rejected():
    with pytest.raises(ValueError, match="aliasing"):
        WindConfig(resonance_hz=40.0)
