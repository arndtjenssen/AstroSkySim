"""Ambient temperature over a night, and the focus drift it causes.

Four decisions carry this module.

**Three temperatures, not one.** ``air_c`` is ambient, ``probe_c`` is what a
sensor on the focuser body reads, ``optics_c`` is the tube and glass - and only
the last of those sets focus. Nothing reports it. That gap is the whole feature:
it is the thermal analogue of ``mount.ra_deg`` versus ``Rig.actual_pointing``, so
a client's temperature compensation has something real to fail against. Driving
focus from ``air_c`` collapses all three into one and a correct coefficient then
works perfectly.

**No history ring, unlike wind.** ``WindModel`` retains every sub-sample because
a smear is an integration over *when the shutter was open*. Temperature is not:
it moves at ~0.3 K/h, so even an SCT at 200 um/K drifts ~5 um across a 300 s sub,
which is nothing against the ``focus_range`` that sets HFD sensitivity. So this
is read once per frame at readout, like every other error term in the simulator.

**It owns its RNG**, seeded from ``cfg.seed`` with a distinct spawn key. Same
reason as ``wind.py``: ``rig.rng`` is drawn from by ``actual_pointing`` on every
read, so a shared stream would depend on how many times unrelated code happened
to touch a property - reproducible in principle and not in practice.

**No step can overshoot, whatever its size.** The baseline is closed-form in
elapsed time, so the night's clock never drifts; the lags, the wander and the
Markov gate all use ``1 - exp(-dt/tau)`` rather than ``dt/tau``. That factor
saturates at 1, so a stalled tick catches up by landing *on* the target rather
than sailing past it and ringing - which is what the Euler form does the moment
``dt`` approaches ``tau``, and it is a 40-minute ``tau`` being caught up here.

This is stricter than ``WindModel._step_weather``, which uses the Euler form and
gets away with it because ``MAX_STEP_S`` bounds a real tick. It is *not* exact
against a moving target: a lag stepped in one go treats the air as held at its
new value for the whole interval. The error is bounded by how far the air moves
within one step: tens of microkelvin over ten minutes of ticking at the 0.5 s
ceiling, and a few hundredths of a degree even across an absurd 600 s catch-up.
On a 20 um/K tube that is under a thousandth of a focuser step.
"""

from __future__ import annotations

import math

import numpy as np

from .config import TemperatureConfig

#: Distinct spawn key for the temperature stream. Spelled out so the seed is
#: legible in a traceback: b"TEMP".
_SEED_SPAWN = 0x54454D50


def _decay(dt: float, tau: float) -> float:
    """Fraction of the way to the target after ``dt`` with time constant ``tau``.

    ``1 - exp(-dt/tau)``, i.e. the exact first-order step, rather than the
    ``dt/tau`` that is only its small-step limit. Total for any ``dt``: it
    saturates at 1 instead of overshooting, so a stalled tick lands on the target
    rather than past it.
    """
    if tau <= 0.0:
        return 1.0
    return -math.expm1(-dt / tau)


class TemperatureModel:
    """Ambient, the probe, the optics, and the focus drift between them."""

    def __init__(self, cfg: TemperatureConfig, seed: int | None) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(None if seed is None else (seed, _SEED_SPAWN))

        #: Seconds since the model started. The night's clock is this plus
        #: ``cfg.hours_into_night``, so a session can open mid-night without a
        #: sun ephemeris anywhere near the tick.
        self.elapsed_s = 0.0

        # Spell state at the Markov chain's *stationary* distribution rather
        # than neutral, for the reason ``WindModel`` starts windy: a session
        # begins in the middle of the weather, and the transient out of a
        # neutral start is long enough to read as a broken feature.
        self.in_spell = bool(self.rng.random() < cfg.spell_probability)
        self.spell_target_c = self._draw_spell() if self.in_spell else 0.0
        self.excursion_c = self.spell_target_c
        self.noise_c = 0.0

        # The lags start settled, so the run does not open with the optics
        # chasing a baseline they were never behind.
        base = self._baseline_c()
        self.air_c = base + self.excursion_c
        self.optics_c = self.air_c
        self.probe_c = self.air_c

    # -- the night ---------------------------------------------------------
    def _baseline_c(self) -> float:
        """Nocturnal cooling curve, closed-form in elapsed time.

        Exponential rather than the Brunt/Groen ``sqrt(t)``: both fit screen-level
        observations, but only this one has a time constant a user can set and
        reason about.
        """
        c = self.cfg
        t_h = c.hours_into_night + self.elapsed_s / 3600.0
        return c.floor_c + c.night_drop_c * math.exp(-t_h / c.tau_hours)

    def _draw_spell(self) -> float:
        """Peak of one spell, signed so warm and cold are equally likely."""
        amp = self.cfg.spell_amplitude_c
        if amp <= 0.0:
            return 0.0
        return float(self.rng.uniform(-amp, amp))

    def _step_spell(self, dt: float) -> None:
        """Two-state Markov gate on whether an air mass is passing over."""
        c = self.cfg
        if c.spell_probability <= 0.0:
            self.in_spell = False
            self.spell_target_c = 0.0
        elif self.in_spell:
            if self.rng.random() < _decay(dt, c.spell_duration_s):
                self.in_spell = False
                self.spell_target_c = 0.0
        else:
            # p is the duty cycle and spell_duration_s the mean on-time, so the
            # off-time follows: mean_off = duration * (1 - p) / p. p < 1 is
            # enforced in the config, which is why this cannot divide by zero.
            mean_off = c.spell_duration_s * (1.0 - c.spell_probability) / c.spell_probability
            if self.rng.random() < _decay(dt, max(mean_off, 1e-9)):
                self.in_spell = True
                self.spell_target_c = self._draw_spell()

        # Ramp rather than step. A discontinuity in the air puts one straight
        # into the focus drift, and a focuser that jumps reads as a bug rather
        # than as weather.
        self.excursion_c += (self.spell_target_c - self.excursion_c) * _decay(dt, c.spell_ramp_s)

    def _step_noise(self, dt: float) -> None:
        """Ornstein-Uhlenbeck wander, exact in both mean and variance."""
        c = self.cfg
        if c.sigma_c <= 0.0:
            self.noise_c = 0.0
            return
        # The exact OU update: the mean decays by ``rho`` and the increment
        # carries the variance the process loses doing it, so the stationary
        # sigma is sigma_c for any dt. The Euler form gets that wrong for large
        # dt and would make the wander depend on the tick rate.
        rho = math.exp(-dt / c.noise_tau_s)
        self.noise_c = rho * self.noise_c + c.sigma_c * math.sqrt(1.0 - rho * rho) * float(
            self.rng.normal()
        )

    def step(self, dt: float) -> None:
        """Advance by ``dt`` seconds of simulated time."""
        if dt <= 0.0:
            return
        self.elapsed_s += dt
        self._step_spell(dt)
        self._step_noise(dt)
        self.air_c = self._baseline_c() + self.excursion_c + self.noise_c
        # The optics and the probe chase the air, each with its own constant.
        # The optics one is the long pole and is what focus follows.
        self.optics_c += (self.air_c - self.optics_c) * _decay(dt, self.cfg.optics_tau_s)
        self.probe_c += (self.air_c - self.probe_c) * _decay(dt, self.cfg.probe_tau_s)

    # -- what the imaging path asks for ------------------------------------
    def focus_drift_steps(self, step_size_um: float) -> float:
        """Steps the focuser is away from focus at the current optics temperature.

        **The sign is the trap.** A positive ``focus_shift_um_per_c`` means
        cooling racks the focuser *out*, to a higher step number, which is the
        direction every measured position-versus-temperature slope reports (an
        Optec coefficient is the negative of the fitted slope; an EdgeHD 14
        measures -35.5 steps/K). So as the optics fall below ``reference_c`` this
        goes positive. Reverse it and the defocus still looks entirely plausible
        in the frame, which is why ``test_cooling_racks_the_focuser_out`` exists.

        Driven by ``optics_c`` and never by ``air_c`` or ``probe_c``: those two
        are what a client can see, and this is what is actually true.
        """
        c = self.cfg
        # reference_c is resolved to start_c by TemperatureConfig's validator;
        # the fallback is only so a hand-built config cannot raise here.
        reference = c.reference_c if c.reference_c is not None else c.start_c
        return (c.focus_shift_um_per_c / step_size_um) * (reference - self.optics_c)


def build_temperature_model(cfg) -> TemperatureModel | None:
    """``TemperatureModel`` if ``[temperature]`` is on, else None.

    None covers the ordinary reason there is no temperature simulation - it is
    switched off - so the rest of the code only ever asks whether it is there,
    the way it does for ``rig.wind`` and ``rig.satellites``.
    """
    if not cfg.temperature.enabled:
        return None
    return TemperatureModel(cfg.temperature, cfg.seed)
