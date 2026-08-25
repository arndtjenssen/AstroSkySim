"""Ambient temperature over a night, and the focus drift it causes.

Three silent failure modes motivate most of the assertions here.

**A reversed sign still defocuses.** ``focus_drift_steps`` moves the focus point
away from where the focuser is parked, and it does that just as convincingly with
the sign backwards - the stars bloat either way, at the same rate, and nothing in
a frame says which direction the tube went. So the sign is asserted directly
against the physical claim (cooling racks the focuser *out*) rather than inferred
from an HFD that rose.

**Collapsing the three temperatures deletes the feature without breaking
anything.** If ``optics_c`` were driven straight from ``air_c`` with no lag, or
if focus were driven from ``probe_c``, every other test here would still pass and
temperature compensation with a correct coefficient would simply start working
perfectly. The gap is asserted on its own.

**An inexact step is invisible at 10 Hz.** The model has to be exact for any
``dt`` so a stalled tick can be caught up in one step. With the stochastic terms
switched off it is fully deterministic, which makes that claim testable as exact
equality rather than as a tolerance.
"""

from __future__ import annotations

import numpy as np
import pytest

from astroskysim.config import Config, TemperatureConfig
from astroskysim.rig import Rig, build_rig
from astroskysim.temperature import TemperatureModel, build_temperature_model


def temp_cfg(**over) -> TemperatureConfig:
    """A cooling night with the stochastic parts quietened by default."""
    base = dict(
        enabled=True,
        start_c=16.0,
        night_drop_c=10.0,
        tau_hours=5.0,
        spell_amplitude_c=0.0,
        spell_probability=0.0,
        sigma_c=0.0,
        optics_tau_s=2400.0,
        probe_tau_s=300.0,
        focus_shift_um_per_c=20.0,
    )
    base.update(over)
    return TemperatureConfig(**base)


def rig_cfg(**over) -> Config:
    """A small rig with everything that competes for the frame switched off."""
    cfg = Config()
    cfg.sensor.width_px = 200
    cfg.sensor.height_px = 160
    cfg.mount.tracking_noise = 0.0
    cfg.mount.periodic_error_amplitude = 0.0
    cfg.temperature = temp_cfg(**over)
    return cfg


def model(**over) -> TemperatureModel:
    return TemperatureModel(temp_cfg(**over), seed=1234)


def run(m: TemperatureModel, seconds: float, dt: float = 0.1) -> None:
    for _ in range(int(seconds / dt)):
        m.step(dt)


# --------------------------------------------------------------------------
# The night. The baseline is closed-form, so these are exact algebra rather
# than a physics disagreement waiting to happen.
# --------------------------------------------------------------------------
def test_the_night_starts_at_the_configured_temperature():
    m = model()
    assert m.air_c == pytest.approx(16.0)
    assert m.optics_c == pytest.approx(16.0)
    assert m.probe_c == pytest.approx(16.0)


def test_the_night_cools_toward_the_configured_floor():
    """The floor is derived from start_c and night_drop_c, never configured."""
    m = model()
    run(m, 12 * 3600.0, dt=10.0)
    # Twelve hours is 2.4 time constants, so ~91% of the way down.
    assert m.air_c == pytest.approx(6.0, abs=1.0)
    assert m.air_c > m.cfg.floor_c


def test_one_time_constant_covers_63_percent_of_the_drop():
    m = model()
    run(m, 5 * 3600.0, dt=10.0)
    assert m.air_c == pytest.approx(16.0 - 10.0 * (1 - np.exp(-1.0)), abs=0.05)


def test_starting_late_in_the_night_starts_cold():
    """hours_into_night is what makes a 03:00 session behave like one."""
    early = model(hours_into_night=0.0)
    late = model(hours_into_night=5.0)
    assert late.air_c < early.air_c - 5.0


def test_the_clock_and_the_air_are_exact_whatever_the_step():
    """Both are closed-form in elapsed time, so stepping cannot make them drift."""
    one, many = model(), model()
    one.step(600.0)
    for _ in range(600):
        many.step(1.0)
    assert one.elapsed_s == pytest.approx(many.elapsed_s)
    assert one.air_c == pytest.approx(many.air_c, abs=1e-12)


def test_a_ticks_worth_of_step_size_makes_no_difference_to_the_lags():
    """The lags are exact against a *held* target, not a moving one.

    So the step size does matter in principle. The error is bounded by how far
    the air moves within one step, and at the tick's MAX_STEP_S ceiling against a
    40-minute lag that is nothing - which is the claim worth pinning, rather than
    an exactness the model does not have.
    """
    one, many = model(), model()
    for _ in range(1200):
        one.step(0.5)
    for _ in range(6000):
        many.step(0.1)
    # Tens of microkelvin over ten minutes, i.e. ~5e-4 um of focus on a 20 um/K
    # tube. Five orders of magnitude below the ~10 um a step is worth.
    assert one.optics_c == pytest.approx(many.optics_c, abs=1e-4)
    assert one.probe_c == pytest.approx(many.probe_c, abs=1e-4)


def test_an_absurd_catch_up_step_lands_on_the_target_rather_than_past_it():
    """``1 - exp(-dt/tau)`` saturates at 1; ``dt/tau`` does not.

    A 600 s stall against a 300 s probe lag would move the Euler form 2x the way
    to the target - overshooting, then oscillating on every subsequent stall.
    """
    m = model()
    m.step(600.0)
    assert m.cfg.floor_c <= m.probe_c <= m.cfg.start_c
    assert m.cfg.floor_c <= m.optics_c <= m.cfg.start_c
    # Still behind the air, never through it.
    assert m.probe_c >= m.air_c
    # And it agrees with a properly stepped run to well under a micron of focus.
    fine = model()
    for _ in range(6000):
        fine.step(0.1)
    assert m.optics_c == pytest.approx(fine.optics_c, abs=0.05)


# --------------------------------------------------------------------------
# Spells and wander.
# --------------------------------------------------------------------------
def test_both_warm_and_cold_spells_occur():
    """The amplitude is signed. A one-sided draw makes the night monotonic."""
    m = model(spell_amplitude_c=3.0, spell_probability=0.4, spell_duration_s=600.0)
    seen = [m._draw_spell() for _ in range(200)]
    assert max(seen) > 0.5
    assert min(seen) < -0.5


def test_a_spell_ramps_rather_than_steps():
    """A discontinuity in the air is a discontinuity in the focus drift."""
    m = model(spell_amplitude_c=3.0, spell_probability=0.4, spell_ramp_s=180.0)
    m.in_spell, m.spell_target_c, m.excursion_c = True, 3.0, 0.0
    m.step(1.0)
    assert 0.0 < m.excursion_c < 0.1  # under a tick's worth of a 180 s ramp


def test_the_wander_has_the_configured_spread():
    """Stationary sigma must not depend on the step size."""
    coarse = model(sigma_c=1.0, noise_tau_s=300.0)
    fine = model(sigma_c=1.0, noise_tau_s=300.0)
    coarse_seen, fine_seen = [], []
    for _ in range(4000):
        coarse.step(100.0)
        coarse_seen.append(coarse.noise_c)
    for _ in range(4000):
        fine.step(10.0)
        fine_seen.append(fine.noise_c)
    assert np.std(coarse_seen) == pytest.approx(1.0, abs=0.15)
    assert np.std(fine_seen) == pytest.approx(1.0, abs=0.15)


# --------------------------------------------------------------------------
# The three temperatures. This is the feature; everything else is scaffolding.
# --------------------------------------------------------------------------
def test_the_optics_lag_the_air_while_it_is_cooling():
    """And the probe sits between them, because it is in the airflow."""
    m = model()
    run(m, 3600.0, dt=1.0)
    assert m.optics_c > m.probe_c > m.air_c
    # An hour and a half into a 40-minute lag, the optics are still well behind.
    assert m.optics_c - m.air_c > 0.5


def test_the_optics_catch_up_once_the_air_settles():
    """A lag, not an offset: nothing here holds the optics permanently warm."""
    m = model()
    run(m, 24 * 3600.0, dt=30.0)
    assert m.optics_c == pytest.approx(m.air_c, abs=0.05)


def test_focus_follows_the_optics_and_not_the_probe():
    """The whole feature in one assertion.

    Compensation reads the probe; focus follows the optics. Drive the drift from
    either of the two visible temperatures and a correct coefficient starts
    working perfectly, which is exactly what this simulator must not do.
    """
    m = model()
    run(m, 3600.0, dt=1.0)
    from_optics = m.focus_drift_steps(1.0)
    from_probe = (m.cfg.focus_shift_um_per_c / 1.0) * (m.cfg.reference_c - m.probe_c)
    assert from_optics != pytest.approx(from_probe, abs=1.0)
    assert from_optics < from_probe  # the optics are warmer, so less drift so far


def test_a_mid_tube_probe_is_configured_by_matching_the_time_constants():
    """probe_tau_s == optics_tau_s is an Optec TCF-SI rather than an EAF."""
    m = model(probe_tau_s=2400.0)
    run(m, 3600.0, dt=1.0)
    assert m.probe_c == pytest.approx(m.optics_c, abs=1e-9)


# --------------------------------------------------------------------------
# Focus drift. The sign is the trap: reversed, it still defocuses.
# --------------------------------------------------------------------------
def test_cooling_racks_the_focuser_out():
    """Positive focus_shift_um_per_c means a *higher* step number as it cools.

    That is the direction every measured position-versus-temperature slope
    reports. Reverse it and the frame looks identical - the stars bloat at the
    same rate - so nothing downstream can catch it.
    """
    m = model()
    run(m, 3 * 3600.0, dt=10.0)
    assert m.optics_c < m.cfg.reference_c
    assert m.focus_drift_steps(1.0) > 0.0


def test_a_negative_coefficient_racks_in():
    m = model(focus_shift_um_per_c=-20.0)
    run(m, 3 * 3600.0, dt=10.0)
    assert m.focus_drift_steps(1.0) < 0.0


def test_a_coarser_focuser_step_is_fewer_steps_for_the_same_micron_shift():
    """step_size_um is the only thing converting the physical shift to steps."""
    m = model()
    run(m, 3 * 3600.0, dt=10.0)
    assert m.focus_drift_steps(2.0) == pytest.approx(m.focus_drift_steps(1.0) / 2.0)


def test_the_drift_is_zero_at_the_reference_temperature():
    m = model(reference_c=16.0)
    assert m.focus_drift_steps(1.0) == pytest.approx(0.0)


def test_the_reference_defaults_to_the_starting_temperature():
    """So a session opens in focus and everything after it is drift."""
    assert temp_cfg(start_c=9.0).reference_c == pytest.approx(9.0)
    assert temp_cfg(start_c=9.0, reference_c=20.0).reference_c == pytest.approx(20.0)


# --------------------------------------------------------------------------
# The rig.
# --------------------------------------------------------------------------
def test_a_default_config_has_temperature_off():
    """Every measured HFD in the suite was taken at a fixed focus."""
    assert Config().temperature.enabled is False
    assert build_temperature_model(Config()) is None
    assert Rig(Config()).temperature is None
    assert Rig(Config()).focus_drift_steps == 0.0


def test_cooling_defocuses_both_chips():
    """The tube expands upstream of the whole train, so the guider goes soft too.

    An off-axis guider's prism is downstream of the focuser, which is why an
    autofocus run bloats the guide star - and thermal drift arrives by the same
    route.
    """
    rig = Rig(rig_cfg())
    before_main, before_guide = rig.current_hfd(), rig.guide_hfd()
    for _ in range(3 * 3600):
        rig.temperature.step(1.0)
    assert rig.focus_drift_steps > 50.0
    assert rig.current_hfd() > before_main + 0.1
    assert rig.guide_hfd() > before_guide + 0.1


def test_a_pinned_guide_hfd_is_immune_to_temperature():
    """A separate guide scope holds its own focus, so it cannot drift."""
    cfg = rig_cfg()
    cfg.optics.guide_hfd_px = 3.5
    rig = Rig(cfg)
    for _ in range(3 * 3600):
        rig.temperature.step(1.0)
    assert rig.guide_hfd() == pytest.approx(3.5)
    assert rig.current_hfd() > 2.4  # the imaging chip still drifted


def test_the_filter_offset_and_the_drift_both_land_on_the_imaging_chip():
    """They add: the guider gets the drift and not the offset."""
    cfg = rig_cfg()
    cfg.filter_wheel.focus_offsets = [0, 0, 0, 0, 120]
    rig = Rig(cfg)
    rig.filter.slot = 5  # Ha
    for _ in range(3600):
        rig.temperature.step(1.0)
    drift = rig.focus_drift_steps
    assert rig.current_hfd() == pytest.approx(
        _hfd(rig, cfg.focuser.perfect_focus + 120 + drift), abs=1e-9
    )
    assert rig.guide_hfd() == pytest.approx(
        _hfd(rig, cfg.focuser.perfect_focus + drift), abs=1e-9
    )


def _hfd(rig: Rig, perfect: float) -> float:
    from astroskysim.sky.render import hfd_from_focus

    return hfd_from_focus(rig.focuser.position, perfect, rig.cfg.focuser.focus_range)


async def test_the_rig_steps_the_model_and_the_cooler_separately():
    """``_step_cooler`` is the sensor; ``self.temperature`` is the air."""
    rig = Rig(rig_cfg())
    rig.camera.cooler_on = True
    rig.camera.set_temperature = -10.0
    for _ in range(600):
        await rig.step(1.0)
    assert rig.camera.temperature == pytest.approx(-10.0, abs=0.5)
    assert rig.temperature.air_c < 16.0
    assert rig.temperature.elapsed_s == pytest.approx(600.0)


def test_the_model_owns_its_rng():
    """Reproducible however many times unrelated code touched a property.

    ``actual_pointing`` draws from ``rig.rng`` on every read, so a temperature
    path sharing that generator would be reproducible in principle and not in
    practice.
    """

    def path(extra_reads: int) -> list[float]:
        rig = build_rig(rig_cfg(spell_probability=0.4, spell_amplitude_c=2.5, sigma_c=0.5))
        out = []
        for _ in range(400):
            rig.temperature.step(1.0)
            for _ in range(extra_reads):
                _ = rig.actual_pointing  # draws from rig.rng on every read
            out.append(rig.temperature.air_c)
        return out

    assert path(0) == path(3)


async def test_a_drifted_frame_records_the_ground_truth():
    """The header carries what no property publishes: the optics temperature."""
    rig = build_rig(rig_cfg())
    for _ in range(2 * 3600):
        rig.temperature.step(1.0)
    rig.camera.exposure_s = 1.0
    rig.capture(rig.camera)
    assert rig.camera.last_air_c == pytest.approx(rig.temperature.air_c)
    assert rig.camera.last_optics_c == pytest.approx(rig.temperature.optics_c)
    assert rig.camera.last_focus_drift == pytest.approx(rig.focus_drift_steps)
    assert rig.camera.last_optics_c > rig.camera.last_air_c


# --------------------------------------------------------------------------
# Config bounds. Each one is asserted on the message, because the reason a
# bound exists is the part that is easy to lose.
# --------------------------------------------------------------------------
def test_a_spell_probability_of_one_is_refused():
    """The Markov off-rate divides by it, exactly as in wind.probability."""
    with pytest.raises(ValueError, match="less than 1"):
        TemperatureConfig(spell_probability=1.0)


def test_the_time_constants_must_be_positive():
    for field in ("tau_hours", "optics_tau_s", "probe_tau_s", "noise_tau_s", "spell_ramp_s"):
        with pytest.raises(ValueError, match="greater than 0"):
            TemperatureConfig(**{field: 0.0})


def test_the_floor_is_derived_and_not_configured():
    assert TemperatureConfig(start_c=16.0, night_drop_c=10.0).floor_c == pytest.approx(6.0)
