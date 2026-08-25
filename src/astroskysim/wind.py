"""Wind, gusts, and the deflection history a mid-exposure smear is built from.

Three decisions carry this module, and each one is easy to undo by accident.

**The model is stateful and stepped, not a function of time.** A gust is a
step into a lightly damped oscillator, and where the mount *is* depends on where
it was - so this cannot be a pure function evaluated at readout the way
``Rig.actual_pointing``'s periodic-error term is. It advances from
``Rig.step(dt)`` and nowhere else.

**It owns its own RNG.** ``rig.rng`` is drawn from by ``actual_pointing`` on
every read, so a wind path sharing it would depend on how many times unrelated
code happened to touch a property - reproducible in principle and not in
practice. The stream is seeded from ``cfg.seed`` with a distinct spawn key, so a
run is reproducible without wind and tracking noise sharing a phase.

**Every sub-sample is retained.** The smear is an integration over *when the
shutter was open*, and it is built after the shutter closed - so the path has to
be recorded as it happens. That is what ``History`` is, and it is why the sample
rate is fixed at ``WIND_DT_SUB`` rather than following the tick: the ring-down is
a few Hz and the tick is 10 Hz, so sampling at the tick rate would alias the
ringing into a slow wobble.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from .config import WIND_DT_SUB, WindConfig

log = logging.getLogger("astroskysim.wind")

#: Air density is folded into ``response_arcsec_at_20kmh``, so this is only the
#: speed that constant is quoted at.
_REFERENCE_KMH = 20.0

#: Ceiling on sub-steps taken in one ``advance`` before falling back to a single
#: exact step. ``MAX_STEP_S = 0.5`` bounds a real tick to ~64, but a test can
#: call ``rig.step`` with anything.
_MAX_SUBSTEPS = 128

#: Distinct spawn key for the wind stream. Spelled out so the seed is legible in
#: a traceback: b"WIND".
_SEED_SPAWN = 0x77494E44

#: Correlation time of the sustained-speed wander, seconds. Real wind is
#: correlated over a minute or so; without this the speed would be white noise
#: about the mean and every gust would look the same.
_OU_TAU_S = 60.0

#: Standard deviation of that wander, as a fraction of ``speed_kmh``.
_OU_SIGMA_FRACTION = 0.25

#: Time constant with which the speed falls away once a windy spell ends. The
#: air does not stop instantly, and a step to zero would ring the mount on the
#: way *down* - an artefact that looks exactly like a real gust.
_CALM_DECAY_S = 10.0

#: Spread of a gust's peak about ``gust_speed_kmh``: uniform on
#: ``[1 - _GUST_SPREAD, 1 + _GUST_SPREAD]``, so repeated gusts are not identical.
_GUST_SPREAD = 0.3


class History:
    """Fixed-capacity ring of ``(d_ra, d_dec)`` deflection samples, arcsec.

    No time column. The elapsed time of sample *i* is ``t0 + i * WIND_DT_SUB``,
    derived on demand - a stored float column would drift against the index the
    window slice is computed from, and cost half again the memory to do it.

    **Read across threads.** ``Rig.step`` writes from the event loop;
    ``Rig.apply_wind_smear`` reads from the readout thread. Preallocating means
    no reallocation and so no crash, but a torn read is possible, so ``window``
    snapshots the write count once and copies what it takes. Guarding this with
    ``rig.capture_lock`` would be worse than the race it fixes: that lock is held
    across a multi-second render, so taking it in the tick reintroduces exactly
    the freeze ``CameraBase.step`` spawns a thread to avoid.
    """

    def __init__(self, history_s: float) -> None:
        self.capacity = max(int(math.ceil(history_s / WIND_DT_SUB)), 2)
        self._ra = np.zeros(self.capacity, dtype=np.float32)
        self._dec = np.zeros(self.capacity, dtype=np.float32)
        #: Total samples ever written. Monotonic, so it doubles as the clock:
        #: sample *i* was taken at ``i * WIND_DT_SUB`` seconds of wind time.
        self.written = 0

    def append(self, d_ra: float, d_dec: float) -> None:
        i = self.written % self.capacity
        self._ra[i] = d_ra
        self._dec[i] = d_dec
        self.written += 1

    @property
    def oldest_index(self) -> int:
        return max(0, self.written - self.capacity)

    def slice(self, start_s: float, end_s: float) -> tuple[np.ndarray, np.ndarray, bool]:
        """Samples whose wind time falls in ``[start_s, end_s]``, plus a flag.

        The flag says the window was clamped because it reached further back
        than the ring retains. The caller warns on it; it must not be swallowed,
        because a quietly shortened window renders a shorter streak, which reads
        as "the wind died down" rather than as a missing history.
        """
        written = self.written  # snapshot once: the tick may be appending
        if written == 0:
            return np.empty(0, np.float64), np.empty(0, np.float64), False

        lo = int(math.floor(start_s / WIND_DT_SUB))
        hi = int(math.ceil(end_s / WIND_DT_SUB))
        oldest = max(0, written - self.capacity)
        clamped = lo < oldest
        lo = max(lo, oldest)
        hi = min(hi, written - 1)
        if hi < lo:
            return np.empty(0, np.float64), np.empty(0, np.float64), clamped

        idx = np.arange(lo, hi + 1) % self.capacity
        # Copy immediately: these are views into a buffer the tick keeps writing.
        return self._ra[idx].astype(np.float64), self._dec[idx].astype(np.float64), clamped


class Window:
    """One exposure's worth of deflection: the mean, and the path about it.

    The split is the whole point. ``mean`` is where the frame is centred, so the
    WCS carries it; ``d_ra``/``d_dec`` are zero-mean, so the smear kernel built
    from them spreads a star without translating it. Compute them from anything
    other than the same slice and a wind-smeared sub is also mis-astrometried -
    two errors that the pixels cannot tell apart.
    """

    __slots__ = ("d_ra", "d_dec", "mean", "clamped")

    def __init__(self, d_ra: np.ndarray, d_dec: np.ndarray, clamped: bool) -> None:
        if d_ra.size:
            mean_ra = float(d_ra.mean())
            mean_dec = float(d_dec.mean())
        else:
            mean_ra = mean_dec = 0.0
        self.mean = (mean_ra, mean_dec)
        self.d_ra = d_ra - mean_ra
        self.d_dec = d_dec - mean_dec
        self.clamped = clamped

    def __len__(self) -> int:
        return int(self.d_ra.size)


class WindModel:
    """Wind speed, the mount's answer to it, and the history of both."""

    def __init__(self, cfg: WindConfig, seed: int | None) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(None if seed is None else (seed, _SEED_SPAWN))
        self.history = History(cfg.history_s)

        #: Wind time, seconds. Tracks ``rig.elapsed_s`` exactly: the sub-step
        #: remainder is carried rather than dropped, because the index-to-time
        #: mapping is what ``window`` slices on.
        self.elapsed_s = 0.0
        self._debt = 0.0

        # Weather state, started at the Markov chain's *stationary*
        # distribution rather than calm.
        #
        # Starting calm is the obvious choice and it is wrong twice over. A
        # session begins in the middle of the weather, not at the beginning of
        # time - and the transient is long: the off-to-on wait averages
        # ``duration_s * (1 - p) / p``, which at the shipped defaults is nearly
        # four minutes of dead air after a user sets ``enabled = true``. That
        # reads as a broken feature, and the frames taken during it are calm-sky
        # frames from a config that asked for wind.
        self.windy = bool(self.rng.random() < cfg.probability)
        self.gusting = False
        self.speed_kmh = cfg.speed_kmh if self.windy else 0.0
        self.gust_kmh = self.speed_kmh

        # Oscillator state per axis: (angle, angular velocity), arcsec. Seeded at
        # the deflection the starting speed sustains, for the same reason: from
        # zero the mount would ring its way up to it, and that ring-down is an
        # artefact of the run beginning rather than of any gust.
        settled = self._target_arcsec()
        w_ra, w_dec = cfg.axis_weights
        self._x = np.array([settled * w_ra, 0.0], dtype=np.float64)  # RA
        self._y = np.array([settled * w_dec, 0.0], dtype=np.float64)  # Dec
        self._step_matrix, self._step_drive = _transition(
            cfg.resonance_hz, cfg.damping, WIND_DT_SUB
        )
        self._warned_clamp = False

    # -- weather -----------------------------------------------------------
    @property
    def deflection(self) -> tuple[float, float]:
        """Current deflection in arcsec, (RA great-circle, Dec)."""
        return float(self._x[0]), float(self._y[0])

    @property
    def reported_gust_kmh(self) -> float:
        """What a weather station would call the gust: the peak on offer now."""
        return self.speed_kmh if not self.gusting else self.gust_kmh

    def _target_arcsec(self) -> float:
        """Static deflection the current speed would settle at."""
        c = self.cfg
        v = self.gust_kmh if self.gusting else self.speed_kmh
        if v <= 0.0:
            return 0.0
        # Aerodynamic pressure goes as v^2; everything else is lumped into the
        # one calibration constant.
        return c.response_arcsec_at_20kmh * (v / _REFERENCE_KMH) ** 2

    def _step_weather(self, dt: float) -> None:
        """Two nested Markov gates: windy/calm, and gusting within windy."""
        c = self.cfg

        # Windy gate. p is the duty cycle, duration_s the mean on-time, so the
        # off-time follows: mean_off = duration_s * (1 - p) / p. p < 1 is
        # enforced in the config, which is why this cannot divide by zero.
        if c.probability <= 0.0:
            self.windy = False
        elif self.windy:
            if self.rng.random() < dt / c.duration_s:
                self.windy = False
        else:
            mean_off = c.duration_s * (1.0 - c.probability) / c.probability
            if self.rng.random() < dt / max(mean_off, 1e-9):
                self.windy = True

        if not self.windy:
            self.gusting = False
            # Relax rather than snap: the air does not stop instantly.
            self.speed_kmh += (0.0 - self.speed_kmh) * min(dt / _CALM_DECAY_S, 1.0)
            self.gust_kmh = self.speed_kmh
            return

        # Sustained speed: Ornstein-Uhlenbeck about the configured mean, so it
        # wanders instead of sitting on a constant. sqrt(dt) on the noise term,
        # for the same reason the buffet term has it - otherwise the sub-step
        # rate would set the gustiness.
        sigma = _OU_SIGMA_FRACTION * c.speed_kmh
        self.speed_kmh += (c.speed_kmh - self.speed_kmh) * (dt / _OU_TAU_S)
        self.speed_kmh += sigma * math.sqrt(2.0 * dt / _OU_TAU_S) * self.rng.normal()
        self.speed_kmh = max(self.speed_kmh, 0.0)

        # Gust gate, nested inside the windy spell.
        if c.gust_probability <= 0.0:
            self.gusting = False
        elif self.gusting:
            if self.rng.random() < dt / c.gust_duration_s:
                self.gusting = False
        else:
            mean_off = c.gust_duration_s * (1.0 - c.gust_probability) / c.gust_probability
            if self.rng.random() < dt / max(mean_off, 1e-9):
                self.gusting = True
                # A gust is a fresh draw, not a ramp: that step is what makes
                # the mount ring, and the ringing is what makes the V-shapes.
                spread = 1.0 - _GUST_SPREAD + 2.0 * _GUST_SPREAD * self.rng.random()
                self.gust_kmh = max(c.gust_speed_kmh * spread, 0.0)
        if not self.gusting:
            self.gust_kmh = self.speed_kmh

    # -- integration -------------------------------------------------------
    def _advance(self, dt: float, matrix: np.ndarray, drive: np.ndarray) -> None:
        """One exact oscillator step toward the current static target."""
        c = self.cfg
        self._step_weather(dt)

        target = self._target_arcsec()
        if c.buffet_fraction > 0.0 and target > 0.0:
            # sqrt(dt), or the gustiness would depend on the sub-step rate and
            # tuning WIND_DT_SUB would silently change the weather.
            target += c.buffet_fraction * target * math.sqrt(dt / WIND_DT_SUB) * self.rng.normal()

        w_ra, w_dec = c.axis_weights
        self._x = matrix @ self._x + drive * (target * w_ra)
        self._y = matrix @ self._y + drive * (target * w_dec)

    def step(self, dt: float) -> None:
        """Advance by ``dt`` seconds of simulated time, recording the path."""
        if dt <= 0.0:
            return
        self._debt += dt
        n = int(self._debt / WIND_DT_SUB)

        if n > _MAX_SUBSTEPS:
            # Catch up in one exact step rather than spinning. The transition
            # matrix is closed-form for any dt, which is the whole reason for
            # using it: a stall costs two exp() calls, and - the part that
            # matters - wind time stays exactly equal to elapsed_s. Dropping
            # sub-steps instead would make the index-to-time mapping that
            # ``window`` slices on quietly wrong.
            matrix, drive = _transition(self.cfg.resonance_hz, self.cfg.damping, self._debt)
            self._advance(self._debt, matrix, drive)
            self.elapsed_s += self._debt
            self._debt = 0.0
            self.history.append(*self.deflection)
            return

        for _ in range(n):
            self._advance(WIND_DT_SUB, self._step_matrix, self._step_drive)
            self.elapsed_s += WIND_DT_SUB
            self._debt -= WIND_DT_SUB
            self.history.append(*self.deflection)

    # -- what the imaging path asks for ------------------------------------
    def window(self, start_s: float, exposure_s: float) -> Window:
        """Deflection over an exposure: the mean, and the zero-mean path.

        ``start_s`` is wind time, i.e. the value ``self.elapsed_s`` held when
        the shutter opened - not "now". The readout runs in a thread and can
        begin minutes after a long sub started.
        """
        if exposure_s <= 0.0:
            return Window(np.empty(0), np.empty(0), False)
        d_ra, d_dec, clamped = self.history.slice(start_s, start_s + exposure_s)
        if clamped and not self._warned_clamp:
            self._warned_clamp = True
            log.warning(
                "a %.0f s exposure is longer than the %.0f s of wind history kept "
                "(wind.history_s); the smear covers only the retained part",
                exposure_s,
                self.cfg.history_s,
            )
        return Window(d_ra, d_dec, clamped)


def _transition(resonance_hz: float, zeta: float, dt: float) -> tuple[np.ndarray, np.ndarray]:
    """Exact state transition for a damped oscillator over a constant drive.

    Solves ``x'' + 2*zeta*w*x' + w**2*x = w**2*u`` over ``dt`` with ``u`` held,
    returning ``(A, b)`` such that ``[x, x'] <- A @ [x, x'] + b * u``.

    Closed form rather than a numerical step for the same reason ``fast_lst_deg``
    is closed form: this runs inside the tick, and being exact for *any* ``dt``
    is what lets a stall be caught up in one step instead of a spin. It is also
    unconditionally stable, so there is no timestep to tune against
    ``resonance_hz``.
    """
    w = 2.0 * math.pi * resonance_hz
    if w <= 0.0 or dt <= 0.0:
        return np.eye(2), np.zeros(2)

    # Underdamped is the only case the config permits (damping < 1), but the
    # general form costs nothing and keeps this honest if that bound moves.
    decay = math.exp(-zeta * w * dt)
    if zeta < 1.0:
        wd = w * math.sqrt(1.0 - zeta * zeta)
        cos, sin = math.cos(wd * dt), math.sin(wd * dt)
        a11 = decay * (cos + zeta * w / wd * sin)
        a12 = decay * sin / wd
        a21 = -decay * (w * w / wd) * sin
        a22 = decay * (cos - zeta * w / wd * sin)
    else:  # pragma: no cover - config forbids it; kept so the algebra is total
        wd = w * math.sqrt(zeta * zeta - 1.0) or 1e-12
        cosh, sinh = math.cosh(wd * dt), math.sinh(wd * dt)
        a11 = decay * (cosh + zeta * w / wd * sinh)
        a12 = decay * sinh / wd
        a21 = -decay * (w * w / wd) * sinh
        a22 = decay * (cosh - zeta * w / wd * sinh)

    matrix = np.array([[a11, a12], [a21, a22]], dtype=np.float64)
    # The particular solution is x = u, x' = 0, so the drive term is whatever
    # the homogeneous part does not already carry.
    drive = np.array([1.0 - a11, -a21], dtype=np.float64)
    return matrix, drive


def path_to_pixels(wcs, d_ra_arcsec: np.ndarray, d_dec_arcsec: np.ndarray):
    """Tangent-plane arcsec offsets to pixel offsets, via the WCS's own matrix.

    ``pixel_scale_matrix`` is the CD matrix astropy derives for either CD or
    PC+CDELT, so the RA flip (``sensor_wcs`` sets ``sx = -scale``) and the
    rotator's position angle come from the same object that built the frame's
    WCS. Restating either one here is four chances to mirror the streak, and a
    mirrored zero-mean smear looks entirely correct.

    Two traps:

    * **No ``cos(dec)``.** The matrix's first axis is already the projection
      plane's east coordinate. ``Rig.actual_pointing`` divides by ``cos(dec)``
      to turn a great-circle offset into RA degrees; that is the only place in
      the feature that does, and doing it twice shrinks the smear away from the
      pole.
    * **Take the WCS, never a plate scale.** The WCS handed in was built with
      this camera's own scale, so a separate guide scope needs no branch here.
      It also describes *unbinned sensor* pixels, which is what the smear runs
      on - before ``subframe`` and ``bin_frame``.

    A full ``wcs_world2pix`` round trip would also work and is affordable, but it
    is worse for the invariant: TAN is not affine, so a path that is exactly
    zero-mean in arcsec is only approximately zero-mean in pixels. This is a
    pure linear map, so zero-mean in means zero-mean out, exactly.
    """
    inv = np.linalg.inv(np.asarray(wcs.pixel_scale_matrix, dtype=np.float64))
    deg = np.stack([np.asarray(d_ra_arcsec), np.asarray(d_dec_arcsec)]) / 3600.0
    dx, dy = inv @ deg
    return dx, dy


def build_wind_model(cfg) -> WindModel | None:
    """``WindModel`` if ``[wind]`` is on, else None.

    None covers the ordinary reason there is no wind - it is switched off - so
    the imaging path only ever asks whether it is there, the way it does for
    ``rig.satellites``.
    """
    if not cfg.wind.enabled:
        return None
    return WindModel(cfg.wind, cfg.seed)
