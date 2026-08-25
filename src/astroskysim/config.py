"""Configuration model. Loaded from TOML, overridable from the CLI."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import tomllib
from pydantic import AfterValidator, BaseModel, Field, model_validator

#: A path from the TOML, with ``~`` expanded. Without this, ``cache_dir =
#: "~/.cache/astroskysim"`` created a directory literally named ``~`` next to the
#: working directory - which looks like a working cache until you go looking
#: for it.
UserPath = Annotated[Path, AfterValidator(lambda p: p.expanduser())]


class SourceMode(str, Enum):
    """How a frame's pixels are produced."""

    #: Stars and deep-sky objects rendered from the local catalogue.
    ARTIFICIAL = "artificial"
    #: A real survey cutout, reprojected onto the sensor grid.
    DSS = "dss"
    #: Survey cutout as background, artificial stars rendered on top.
    COMPOSITE = "composite"


class Site(BaseModel):
    latitude: float = 52.37
    longitude: float = 4.90  # East positive
    elevation: float = 10.0


class Telescope(BaseModel):
    focal_length_mm: float = Field(700.0, gt=0)
    aperture_mm: float = Field(100.0, gt=0)
    #: The guide scope. Leave unset for an off-axis guider, which looks through
    #: the imaging OTA and so shares its focal length and aperture. INDI has had
    #: somewhere to report these all along (``TELESCOPE_INFO`` carries
    #: ``GUIDER_FOCAL_LENGTH``/``GUIDER_APERTURE``); before this they just echoed
    #: the main scope.
    guide_focal_length_mm: float | None = Field(None, gt=0)
    guide_aperture_mm: float | None = Field(None, gt=0)

    @property
    def separate_guide_scope(self) -> bool:
        return self.guide_focal_length_mm is not None

    @property
    def guide_focal_length(self) -> float:
        return self.guide_focal_length_mm or self.focal_length_mm

    @property
    def guide_aperture(self) -> float:
        return self.guide_aperture_mm or self.aperture_mm


class Sensor(BaseModel):
    width_px: int = Field(1200, ge=16, le=32768)
    height_px: int = Field(1000, ge=16, le=32768)
    pixel_size_um: float = Field(5.0, gt=0)
    #: 0 = mono, otherwise a Bayer pattern name.
    bayer: Literal["MONO", "RGGB", "BGGR", "GRBG", "GBRG"] = "MONO"
    bit_depth: Literal[8, 16] = 16
    well_depth_e: float = Field(20000.0, gt=0)
    read_noise_e: float = Field(3.0, ge=0)
    #: e-/ADU at gain 100 (unity-ish). Sets where the electron count lands in ADU.
    e_per_adu: float = Field(1.0, gt=0)
    hot_pixels: int = Field(0, ge=0)
    #: Dark current of a hot pixel, e-/s. Scaled by exposure time, so short
    #: guide frames are not dominated by fixed-pattern noise.
    hot_pixel_e_s: float = Field(200.0, ge=0)


class Focuser(BaseModel):
    max_position: int = Field(30000, gt=0)
    perfect_focus: int = Field(15000, ge=0)
    #: Position offset producing the configured HFD range; controls how fast
    #: the star bloats as you defocus.
    focus_range: int = Field(1000, gt=0)
    backlash: int = Field(0, ge=0)
    #: Focuser travel per step, um. Load-bearing: it is what turns
    #: ``temperature.focus_shift_um_per_c`` into steps, so a rig with a 2 um
    #: step drifts half as many steps for the same physical shift. Real values
    #: run 0.1-4 um depending on motor and pitch (an Optec TCF-S is 2.16).
    step_size_um: float = Field(1.0, gt=0)
    speed_steps_s: float = Field(2000.0, gt=0)
    #: Fixed ambient temperature reported when ``[temperature]`` is off, so a
    #: client still sees a plausible reading rather than nothing.
    temperature: float = 12.0
    #: Steps per degree C the *client's* compensation applies, when it enables
    #: ``FOCUS_TEMPERATURE_COMPENSATION``.
    #:
    #: Deliberately independent of ``temperature.focus_shift_um_per_c``, which is
    #: what the telescope actually does. One is the correction, the other is the
    #: error, and keeping them separate is what lets a mis-calibrated coefficient
    #: over- or under-correct. ``focus_shift_um_per_c / step_size_um`` is the
    #: perfectly calibrated value - and it still under-corrects, because the
    #: probe reads the air and focus follows the optics.
    temp_coeff: float = 0.0


class Rotator(BaseModel):
    speed_deg_s: float = Field(4.0, gt=0)
    #: Offset between mechanical and sky position angle, in degrees.
    mechanical_offset: float = 0.0
    reversed: bool = False


class FilterWheel(BaseModel):
    names: list[str] = Field(default_factory=lambda: ["L", "R", "G", "B", "Ha"])
    #: Per-filter focus offsets in steps, so a per-filter autofocus run has
    #: something real to find.
    focus_offsets: list[int] = Field(default_factory=lambda: [0, 0, 0, 0, 120])
    #: Per-filter transmission as a fraction of the unfiltered throughput.
    #: Multiplies the *whole* light path - stars, nebulosity and sky alike, as
    #: a real filter does - so a narrowband sub is genuinely starved and needs
    #: the exposure to match. Unset means 1.0 everywhere, which is what every
    #: config before this got. Sensible broadband values are 1.0 for L and
    #: ~0.3 for each of R/G/B; a 3 nm Ha passes ~0.01 of a luminance band.
    transmission: list[float] | None = None
    change_time_s: float = Field(1.5, ge=0)

    @model_validator(mode="after")
    def _same_length(self) -> FilterWheel:
        if len(self.focus_offsets) != len(self.names):
            raise ValueError(
                f"focus_offsets has {len(self.focus_offsets)} entries "
                f"but there are {len(self.names)} filters"
            )
        if self.transmission is not None and len(self.transmission) != len(self.names):
            raise ValueError(
                f"transmission has {len(self.transmission)} entries "
                f"but there are {len(self.names)} filters"
            )
        return self

    def transmission_for(self, slot: int) -> float:
        """Transmission of a 1-based filter slot, clamped to the wheel."""
        if not self.transmission:
            return 1.0
        idx = max(0, min(slot - 1, len(self.transmission) - 1))
        return max(self.transmission[idx], 0.0)


class MountConfig(BaseModel):
    equatorial: bool = True
    slew_rate_deg_s: float = Field(3.0, gt=0)
    #: Guide rate as a fraction of sidereal.
    guide_rate: float = Field(0.5, gt=0, le=1.0)
    #: Polar alignment error, arcminutes.
    azimuth_error: float = 0.0
    elevation_error: float = 0.0
    #: Periodic error amplitude (arcsec) and period (s).
    periodic_error_amplitude: float = 0.0
    periodic_error_period: float = 480.0
    #: Random tracking noise, arcsec RMS.
    tracking_noise: float = 0.0
    park_ra_hours: float = 0.0
    park_dec_deg: float = 90.0


#: Sub-step of the wind integrator, seconds. Fixed rather than tied to the tick,
#: because the mount's ring-down is a few Hz and the tick is 10 Hz: sampling the
#: oscillator at the tick rate would alias the ringing into a slow wobble. Also
#: the aliasing bound behind ``WindConfig.resonance_hz``'s ceiling.
WIND_DT_SUB = 1.0 / 128.0


class WindConfig(BaseModel):
    """Wind, gusts, and the mid-exposure smear they leave in a sub.

    Without a dome the rig is exposed. Wind pushes the tube - continuously, and
    in gusts - and the mount deflects. Two things follow, and both are what this
    section exists to produce:

    * The guide star moves, so a client's guider sees spikes and fights them.
      Sustained push gets guided out with a lag; the ring-down after a gust is
      above any guider's correction bandwidth and does not.
    * The shift happens *while the shutter is open*, so the star is not a
      displaced disc - it is a streak, a smear or a V, depending on where in the
      exposure the gust landed and how the mount rang down afterwards.

    The dependence on focal length needs no parameter: the deflection is angular,
    and the plate scale turns it into pixels. On 3.76 um pixels the same 1.5"
    shake is 0.8 px at 400 mm and 3.9 px at 2000 mm.
    """

    #: Off by default, and that is load-bearing rather than cautious: every
    #: measured number in the test suite was taken against a still sky, so a
    #: default of ``true`` would move every frame in every existing test.
    enabled: bool = False

    # -- the weather -------------------------------------------------------
    #: Mean sustained speed while it is actually blowing, km/h.
    speed_kmh: float = Field(12.0, ge=0)
    #: Fraction of the session that is windy at all. The calm/windy gate is a
    #: two-state Markov chain, so the off-rate is ``duration_s * (1 - p) / p``
    #: and ``p = 1.0`` would divide by zero - hence ``lt``, rather than a
    #: special case for "always windy".
    probability: float = Field(0.4, ge=0.0, lt=1.0)
    #: Mean length of one windy spell, seconds.
    duration_s: float = Field(120.0, gt=0)
    #: Peak speed in a gust, km/h. Validated to be at least ``speed_kmh``: a
    #: gust that is slower than the wind it interrupts is a lull, and the model
    #: would quietly render the opposite of what the name says.
    gust_speed_kmh: float = Field(35.0, ge=0)
    #: Fraction of a *windy* spell spent gusting. Gusts do not fire on a
    #: dead-calm night.
    gust_probability: float = Field(0.15, ge=0.0, lt=1.0)
    #: Mean gust length, seconds. Short next to ``duration_s`` by nature.
    gust_duration_s: float = Field(2.5, gt=0)

    # -- how the mount answers --------------------------------------------
    #: Deflection in arcsec at a sustained 20 km/h, scaling as v^2 because
    #: aerodynamic pressure does.
    #:
    #: This is a **lumped** compliance: sail area, drag coefficient, lever arm
    #: and torsional stiffness collapsed into one number, because four of those
    #: five are unknowable for a real rig and would become fudge factors with
    #: units. Calibrate it against what the rig actually does in a breeze.
    #:
    #: The unit trap is arcsec-versus-pixels. 1.2" is under a pixel on a short
    #: refractor (1.94"/px at 400 mm) and over three on an SCT (0.39"/px at
    #: 2000 mm), so the same number is invisible on one rig and ruins subs on
    #: another. ``build_rig`` logs the pixel equivalent at startup.
    response_arcsec_at_20kmh: float = Field(1.2, ge=0)
    #: Ring-down frequency of the tube on the mount, Hz. **This is what makes
    #: the V-shapes**: a gust is a step, and a step into a lightly damped
    #: oscillator overshoots and rings rather than settling.
    #:
    #: The ceiling is enforced in ``_bounds`` rather than with an ``le`` here, so
    #: the error can say *why* 40 Hz is refused. A bare "less than or equal to
    #: 16" reads as an arbitrary limit; it is in fact the point past which the
    #: integrator aliases the ring-down into a slow wobble.
    resonance_hz: float = Field(4.0, gt=0)
    #: Damping ratio of that ring-down. Bounded **below 1 on purpose**: at
    #: zeta >= 1 the mount is critically or over-damped, so there is no
    #: overshoot, no ringing and no V-shape, and the whole feature silently
    #: degrades to a slow pointing offset with nothing in the log to say so.
    #: A tube on a mount is 0.03-0.15.
    damping: float = Field(0.15, gt=0, lt=1.0)
    #: How the deflection splits across the mechanical axes, RA:Dec. RA is the
    #: softer axis - it is the driven one, fighting the worm - which is why wind
    #: spikes land mostly in RA on real guide logs.
    #:
    #: The name alone does not say whether this is an amplitude or a power
    #: ratio, so: it is amplitude, and the split preserves the vector
    #: magnitude. ``theta_ra = theta * r / sqrt(1 + r**2)`` and
    #: ``theta_dec = theta / sqrt(1 + r**2)``.
    axis_ratio_ra_dec: float = Field(1.5, gt=0)
    #: High-frequency buffeting, as a fraction of the steady push. Without it
    #: the trace between gusts is unnaturally clean.
    buffet_fraction: float = Field(0.25, ge=0)

    # -- bookkeeping -------------------------------------------------------
    #: Seconds of deflection history retained, which is what bounds the longest
    #: exposure that can be smeared correctly. The smear is built *after* the
    #: shutter closed, so this has to cover the exposure plus however long the
    #: frame sat in the readout queue - hence the slack over a 600 s sub.
    #: At 128 Hz, 900 s is ~115k samples in two float32 columns, under a MB.
    history_s: float = Field(900.0, gt=0, le=7200.0)

    @model_validator(mode="after")
    def _bounds(self) -> WindConfig:
        if self.gust_speed_kmh < self.speed_kmh:
            raise ValueError(
                f"wind.gust_speed_kmh ({self.gust_speed_kmh:g}) is below "
                f"wind.speed_kmh ({self.speed_kmh:g}); a gust slower than the "
                "sustained wind is a lull, not a gust"
            )
        # Nyquist is 64 Hz here, but a straight-line path between samples needs
        # far more than two per cycle before a ring-down stops looking like a
        # polygon. Eight is the point where the V's tips survive.
        limit = 1.0 / (8.0 * WIND_DT_SUB)
        if self.resonance_hz > limit:
            raise ValueError(
                f"wind.resonance_hz ({self.resonance_hz:g}) exceeds {limit:g} Hz, "
                f"the most the {1 / WIND_DT_SUB:g} Hz integrator can represent "
                "without aliasing the ring-down"
            )
        return self

    @property
    def axis_weights(self) -> tuple[float, float]:
        """RA and Dec shares of a deflection, preserving its magnitude."""
        r = self.axis_ratio_ra_dec
        norm = (1.0 + r * r) ** 0.5
        return r / norm, 1.0 / norm


class TemperatureConfig(BaseModel):
    """Ambient temperature over a night, and the focus drift it causes.

    A night cools. Dusk to dawn is 5-15 K at a lowland site, and warm and cold
    air masses pass over on top of that. Focus follows, because the tube
    lengthens and shortens and the glass changes index: 15-25 um/K on an
    aluminium-tube refractor, 150-350 um/K on a Schmidt-Cassegrain, where the
    secondary amplifies the primary-secondary spacing change by m^2 ~ 25. So an
    autofocus run goes stale, and the staler it gets the softer the subs.

    **Three temperatures, and the gap between them is the feature.** This is the
    thermal analogue of ``mount.ra_deg`` versus ``actual_pointing``:

    * ``air_c`` - ambient, what ``WEATHER_TEMPERATURE`` reports.
    * ``probe_c`` - a sensor on the focuser body, what ``FOCUS_TEMPERATURE``
      reports. Lags the air a little.
    * ``optics_c`` - the tube and glass, which nothing reports and which is what
      actually sets focus. Lags a lot more.

    A client that calibrates ``focuser.temp_coeff`` against the probe and
    compensates perfectly *still* drifts, because focus follows the optics. That
    is the real, widely-misdiagnosed failure - a ZWO EAF's sensor sits at the
    focuser, not on the tube - and it is what makes temperature compensation
    worth testing against. Drive focus from ``air_c`` instead and a correct
    coefficient works perfectly, leaving nothing to find.
    """

    #: Off by default, for the same reason ``wind.enabled`` is: every measured
    #: HFD in the test suite was taken at a fixed focus, so a default of ``true``
    #: would drift all of them.
    enabled: bool = False

    # -- the night ---------------------------------------------------------
    #: Ambient when the session opens, degrees C.
    start_c: float = 16.0
    #: Total fall from ``start_c`` toward the asymptote. The floor is derived
    #: (``start_c - night_drop_c``) rather than configured, so the two cannot be
    #: set inconsistently.
    night_drop_c: float = Field(10.0, ge=0)
    #: Time constant of that fall, hours. Measured nocturnal-boundary-layer
    #: constants run 3-8 h, shortest in enclosed basins and longest over plains.
    tau_hours: float = Field(5.0, gt=0)
    #: How far into the night the session starts, hours. 0 opens at dusk with
    #: the whole drop ahead; 3 opens most of the way down it. This is what makes
    #: a 03:00 session behave like one, without a sun ephemeris in the tick.
    hours_into_night: float = Field(0.0, ge=0)

    # -- spells and wander -------------------------------------------------
    #: Peak excursion of a warm or cold spell, degrees C. **Signed per spell**:
    #: the draw is symmetric about zero, so warm spells are as common as cold
    #: ones and the night is not a monotonic slide.
    spell_amplitude_c: float = Field(2.5, ge=0)
    #: Fraction of the session spent in a spell. The gate is a two-state Markov
    #: chain whose off-rate divides by this, so ``1.0`` would divide by zero -
    #: hence ``lt``, exactly as in ``wind.probability``.
    spell_probability: float = Field(0.25, ge=0.0, lt=1.0)
    #: Mean length of one spell, seconds.
    spell_duration_s: float = Field(900.0, gt=0)
    #: How fast a spell arrives, seconds. Air does not step: a discontinuity
    #: here would put one straight into the focus drift, and a focuser that
    #: jumps is a bug report rather than weather.
    spell_ramp_s: float = Field(180.0, gt=0)
    #: Background wander, degrees C RMS. Measured per-night sigma is 0.77 at La
    #: Palma and 1.30 at Macon; a lowland back garden sits at the top of that.
    #: Small by default so the spells stay legible against it.
    sigma_c: float = Field(0.15, ge=0)
    #: Correlation time of that wander, seconds. Minutes, not seconds: mesoscale
    #: fluctuations are not universal the way microscale turbulence is, and one
    #: minute is the usual averaging compromise.
    noise_tau_s: float = Field(300.0, gt=0)

    # -- what lags what ----------------------------------------------------
    #: Time constant with which the optics follow the air, seconds. ~40 min for
    #: a refractor's lens cell; hours for a thick mirror, and longer still for a
    #: Maksutov meniscus at ~10% of aperture. Cooling is limited by conduction
    #: through the glass, not by airflow past it, which is why a fan helps far
    #: less than its size suggests.
    optics_tau_s: float = Field(2400.0, gt=0)
    #: Time constant of the focuser's own temperature probe, seconds. Short,
    #: because it sits in the airflow rather than in the glass. Setting it equal
    #: to ``optics_tau_s`` models a mid-tube probe (an Optec TCF-SI) instead,
    #: which is why one rig calibrates reproducibly and another does not.
    probe_tau_s: float = Field(300.0, gt=0)

    # -- how much focus a degree costs -------------------------------------
    #: Focuser travel per degree of cooling, um/K. **Signed, and the sign is the
    #: trap**: positive means cooling racks the focuser *out*, to a higher step
    #: number, which is the direction every measured slope reports.
    #:
    #: One lumped constant, like ``wind.response_arcsec_at_20kmh``, standing in
    #: for tube CTE, glass dn/dT and secondary amplification together. Not
    #: derived from a tube-material setting on purpose: the Cassegrain
    #: amplification is m^2 and m runs 1.5-5 across designs, so a single
    #: "cassegrain" constant would be wrong for most rigs. Measured and inferred
    #: values, and ``build_rig`` logs what yours costs in HFD:
    #:
    #: ===========================  =========  ================================
    #: tube                         um/K       basis
    #: ===========================  =========  ================================
    #: refractor, aluminium         15-25      measured; CTE 23 ppm/K x f
    #: refractor, carbon            5-12       inference; only the glass is left
    #: Newtonian, aluminium         20-25      CTE x f, no amplification
    #: Newtonian, steel / carbon    10-13 / 2-6  CTE 12 / ~0-1 ppm/K
    #: Maksutov                     150-300    **inference only**, m ~ 6
    #: SCT, C8 f/10                 150-260    three independent routes agree
    #: SCT, C11 / C14               ~300 / ~350  C14 measured at 161 steps/K
    #: ===========================  =========  ================================
    #:
    #: Converted to steps through ``focuser.step_size_um``.
    focus_shift_um_per_c: float = 20.0
    #: Temperature at which ``focuser.perfect_focus`` really is perfect.
    #: Defaults to ``start_c``, so a session opens in focus and everything after
    #: it is drift.
    reference_c: float | None = None

    @model_validator(mode="after")
    def _resolve_reference(self) -> TemperatureConfig:
        if self.reference_c is None:
            self.reference_c = self.start_c
        return self

    @property
    def floor_c(self) -> float:
        """Asymptote of the cooling curve; derived, never configured."""
        return self.start_c - self.night_drop_c


class Optics(BaseModel):
    seeing_arcsec: float = Field(2.5, gt=0)
    #: Sky brightness in mag/arcsec^2 - an SQM reading. Converted to e-/px/s
    #: through the aperture, plate scale and throughput below, so a change of
    #: telescope or of camera moves the background with it. 21.0 is a decent
    #: rural site, 20.0 suburban, 18.0 a city centre.
    sky_mag_arcsec2: float = Field(21.0, gt=0)
    #: Sky background straight in e-/px/s, overriding ``sky_mag_arcsec2``.
    #: This is the field that used to be called *the* sky background, and its
    #: unit is the trap: ``sky_background = 21.0`` looks like an SQM reading
    #: and is in fact roughly SQM 18 on a small refractor. Prefer
    #: ``sky_mag_arcsec2``; ``build_rig`` warns when this override is in use.
    sky_background: float | None = Field(None, ge=0)
    #: End-to-end optical + quantum efficiency, before any filter. One number
    #: standing in for coatings, obstruction losses and QE.
    throughput: float = Field(0.5, gt=0, le=1.0)
    #: e-/s/m^2 from a magnitude 0 source, integrated over the band. The
    #: anchor for stars, sky and survey cutouts alike - move it and the whole
    #: photometric scale moves together.
    zero_point_e_s_m2: float = Field(1.0e10, gt=0)
    #: Fixed HFD (px) for the guide camera. A separate guide scope is focused
    #: once and left alone, so the main focuser must not blur the guide star -
    #: otherwise an autofocus run bloats it to 10 px and guiding is lost
    #: mid-sequence, which no real rig does. Unset, the guider follows the
    #: focuser, which is right for an off-axis guider.
    guide_hfd_px: float | None = Field(None, gt=0)


class ArtificialSource(BaseModel):
    #: Directory holding the HNSKY ``.290`` star database files.
    catalog_dir: UserPath | None = None
    #: Preferred database prefix, e.g. "g14". Falls back through known names.
    catalog: str = "g14"
    limiting_mag: float = 16.0
    #: Used when no catalogue files are present, so the simulator still runs.
    allow_synthetic_fallback: bool = True


class SurveyLayer(BaseModel):
    """One survey and the photometric anchor that turns it into electrons.

    A ``[source.dss.per_filter.<name>]`` section is one of these, attached to
    the filter of that name. The anchor is what makes the layers comparable:
    ``ref_mag_arcsec2`` is the surface brightness that the reference level of
    the background-subtracted cutout stands for, and everything downstream -
    aperture, plate scale, throughput, exposure - follows from the optics.

    The reference level itself comes from one of two places:

    * ``ref_value`` - a raw survey pixel value above the survey's own sky. Use
      this whenever the survey is linear and internally calibrated, because it
      preserves *both* the ratios between its bands and the difference between
      a bright target and a faint one. The three NSNS line maps share one
      scale, so giving Ha, OIII and SII the same ``ref_value`` reproduces the
      real line ratios: an HII region comes out Ha-dominated and a planetary
      nebula OIII-dominated, without either being configured that way.
    * ``ref_percentile`` - the fallback when the units are unknown or the
      response is non-linear, as on the photographic DSS plates. It normalises
      each cutout against itself, which is robust but flattens the sky: an
      empty field then renders as brightly as M42.

    Reprojection preserves surface brightness rather than flux, so a
    ``ref_value`` calibrated once holds for any sensor and plate scale.
    """

    #: ``hips:<HiPS id>``, ``skyview:<Survey>`` or ``eso:<Sky-Survey>``.
    survey: str = "hips:CDS/P/DSS2/red"
    ref_mag_arcsec2: float = Field(19.5, gt=0)
    ref_percentile: float = Field(99.0, gt=50.0, lt=100.0)
    #: Absolute anchor in the survey's own units, above its own sky. Overrides
    #: ``ref_percentile`` when set.
    ref_value: float | None = Field(None, gt=0)
    #: True when this survey *is* the object as the filter sees it, which is
    #: the point of attaching it to that filter. The filter's ``transmission``
    #: is a broadband fraction: right for a star or the sky, whose light the
    #: filter throws away, and wrong for an image already taken in that band -
    #: a 3 nm Ha filter does not dim an Ha map by fifty. So an in-band layer is
    #: exempt from it, while stars and sky are not. Set false to model imaging
    #: a broadband proxy through a narrow filter, which *is* attenuated.
    in_band: bool = True


class DssSource(BaseModel):
    #: ``hips:<HiPS id>``, ``skyview:<Survey>`` or ``eso:<Sky-Survey>``.
    #: hips2fits is the default: it reaches every HiPS on the CDS list, including
    #: the narrowband ones no other back end here can serve.
    #:
    #: This is the *default* layer: the guide camera always uses it (its pickoff
    #: prism is upstream of the filter wheel), and so does any filter without a
    #: ``per_filter`` entry.
    survey: str = "hips:CDS/P/DSS2/red"
    cache_dir: UserPath | None = None
    timeout_s: float = Field(60.0, gt=0)
    #: hips2fits hosts, tried in order; unset uses ``dss.HIPS_BASES``. The two
    #: CDS machines fail independently - one has answered the TCP connection and
    #: then gone silent for minutes while the other served the same cutout - so
    #: the list is a failover chain, not a preference.
    hips_bases: list[str] | None = None
    #: Socket timeout for a host that still has an alternate behind it, so a
    #: silent host costs this rather than the full ``timeout_s``.
    hips_probe_timeout_s: float = Field(15.0, gt=0)
    #: Fall back to the artificial sky if the fetch fails, rather than erroring.
    fallback_to_artificial: bool = True
    #: Refuse a cutout covering less than this fraction of the sensor. Partial-sky
    #: surveys, and surveys that mask their saturated cores, otherwise deliver a
    #: frame that is mostly black hole where the target should be.
    min_coverage: float = Field(0.5, ge=0.0, le=1.0)
    #: Ceiling on the pixel grid requested from hips2fits, which resamples to
    #: whatever it is asked for.
    max_download_px: int = Field(3000, ge=300, le=10000)
    #: Photometric anchor for the cutout: the surface brightness, in
    #: mag/arcsec^2, that the reference level of the background-subtracted
    #: survey pixels stands for. Everything else - aperture, plate scale,
    #: throughput, filter, exposure - follows from the optics. Raise the
    #: magnitude to dim the nebulosity. See ``SurveyLayer``.
    ref_mag_arcsec2: float = Field(19.5, gt=0)
    ref_percentile: float = Field(99.0, gt=50.0, lt=100.0)
    ref_value: float | None = Field(None, gt=0)
    #: The default layer is a stand-in for whatever filter is in the beam, not
    #: a match for it, so it *is* attenuated by that filter's transmission.
    #: Per-filter layers default the other way. See ``SurveyLayer.in_band``.
    in_band: bool = False
    #: Survey per filter, keyed by the name in ``[filter_wheel] names``.
    #: Unlisted filters fall back to the default layer above.
    per_filter: dict[str, SurveyLayer] = Field(default_factory=dict)

    @property
    def default_layer(self) -> SurveyLayer:
        return SurveyLayer(
            survey=self.survey,
            ref_mag_arcsec2=self.ref_mag_arcsec2,
            ref_percentile=self.ref_percentile,
            ref_value=self.ref_value,
            in_band=self.in_band,
        )


class CompositeSource(BaseModel):
    #: Weight of the survey background when blended under artificial stars.
    background_weight: float = Field(1.0, ge=0)
    #: Weight of the rendered artificial stars.
    star_weight: float = Field(1.0, ge=0)
    #: Subtract an estimated stellar component from the survey background so
    #: real stars do not double up with the rendered ones.
    suppress_background_stars: bool = True


class SourceConfig(BaseModel):
    mode: SourceMode = SourceMode.ARTIFICIAL
    artificial: ArtificialSource = Field(default_factory=ArtificialSource)
    dss: DssSource = Field(default_factory=DssSource)
    composite: CompositeSource = Field(default_factory=CompositeSource)


class SatellitesRef(BaseModel):
    """A rig config's pointer to the *shared* satellite configuration.

    The source list, the element cache and the photometry live in their own
    file (``satellites/config.py``), because which satellites are in orbit is a
    property of the machine and the week rather than of a telescope: one
    download and one list serve every ``sim.toml`` on the box. All a rig config
    gets is where to look and whether to look at all.

    ``enabled = false`` switches trails off for this rig. There is no
    ``enabled = true``: with nothing fetched there is nothing to switch on, and
    "does this machine have satellites" is the shared file's decision.
    """

    #: Overrides the search path. A path that does not exist is an error, not a
    #: fall-through to the defaults.
    config: UserPath | None = None
    enabled: bool | None = None


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(7625, ge=1, le=65535)
    tick_hz: float = Field(10.0, gt=0, le=100)
    device_prefix: str = "AstroSkySim"
    #: Which devices to advertise.
    mount: bool = True
    camera: bool = True
    guide_camera: bool = True
    focuser: bool = True
    rotator: bool = True
    filter_wheel: bool = True
    #: The weather station, reporting ``[wind]``. Off by default, and not for
    #: caution: a client's profile enumerates devices, so defaulting this on
    #: would add an unexpected seventh device to every existing Ekos profile.
    #: Deliberately *not* derived from ``wind.enabled`` - two sources of truth
    #: for one switch is how a device ends up half-present. ``build_rig`` logs a
    #: pointer when the wind is blowing and nothing is reporting it.
    weather: bool = False


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    site: Site = Field(default_factory=Site)
    telescope: Telescope = Field(default_factory=Telescope)
    sensor: Sensor = Field(default_factory=Sensor)
    #: The guide camera's sensor. Real guide cameras are small, uncooled and
    #: coarse-pixelled next to the imaging chip, so sharing one spec makes the
    #: two cameras indistinguishable to a client. Left unset the guider falls
    #: back to ``sensor``, which is what every config before this did.
    sensor_guide_cam: Sensor | None = None
    focuser: Focuser = Field(default_factory=Focuser)
    rotator: Rotator = Field(default_factory=Rotator)
    filter_wheel: FilterWheel = Field(default_factory=FilterWheel)
    mount: MountConfig = Field(default_factory=MountConfig)
    #: Wind, gusts, and the mid-exposure smear they leave. Off by default.
    wind: WindConfig = Field(default_factory=WindConfig)
    #: Ambient temperature over the night, and the focus drift it causes. Off by
    #: default.
    temperature: TemperatureConfig = Field(default_factory=TemperatureConfig)
    optics: Optics = Field(default_factory=Optics)
    source: SourceConfig = Field(default_factory=SourceConfig)
    #: Where the shared satellite configuration lives, and whether to use it.
    #: Everything else about satellites is in that file, not this one.
    satellites: SatellitesRef = Field(default_factory=SatellitesRef)
    #: Fixed RNG seed makes a run reproducible; None seeds from the OS.
    seed: int | None = 1234

    @model_validator(mode="after")
    def _per_filter_names_exist(self) -> Config:
        """A ``per_filter`` key must name a real filter.

        Silently ignoring a typo is the worst outcome: the filter quietly keeps
        the default broadband survey and the frames look almost right, so the
        mistake surfaces as "my Ha subs are too bright" weeks later.
        """
        unknown = [n for n in self.source.dss.per_filter if n not in self.filter_wheel.names]
        if unknown:
            raise ValueError(
                f"source.dss.per_filter names no such filter: {', '.join(unknown)}; "
                f"filter_wheel.names are {', '.join(self.filter_wheel.names)}"
            )
        return self

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None:
            return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    # -- derived -----------------------------------------------------------
    @property
    def guide_sensor(self) -> Sensor:
        """The guide camera's sensor, or the imaging one if none is configured."""
        return self.sensor_guide_cam or self.sensor

    @staticmethod
    def _scale(pixel_size_um: float, focal_length_mm: float) -> float:
        return 206.264806 * pixel_size_um / focal_length_mm

    @property
    def scale_arcsec_px(self) -> float:
        """Plate scale of one unbinned imaging pixel."""
        return self._scale(self.sensor.pixel_size_um, self.telescope.focal_length_mm)

    @property
    def guide_scale_arcsec_px(self) -> float:
        """Plate scale of one unbinned guide pixel, through the guide scope."""
        return self._scale(self.guide_sensor.pixel_size_um, self.telescope.guide_focal_length)

    @property
    def fov_deg(self) -> tuple[float, float]:
        s = self.scale_arcsec_px / 3600.0
        return self.sensor.width_px * s, self.sensor.height_px * s

    @property
    def guide_fov_deg(self) -> tuple[float, float]:
        s = self.guide_scale_arcsec_px / 3600.0
        return self.guide_sensor.width_px * s, self.guide_sensor.height_px * s
