"""The simulated rig: one shared object holding all physical state.

Devices are thin protocol adapters over this. Keeping the physics here rather
than in the device objects is what lets two INDI clients see one telescope
(``indi/device.py`` explains why one device object is shared by all clients).

The distinction that makes the simulator useful: the mount *reports* the
commanded position, while the camera images the *actual* position. Polar
misalignment, periodic error and tracking noise live in the gap between them, so
guiding and plate-solve-and-centre loops have something real to correct.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from astropy.time import Time

from .config import Config
from .sky.render import (
    Optics,
    SensorModel,
    add_hot_pixels,
    add_sky_and_noise,
    apply_bayer,
    apply_smear,
    bin_frame,
    hfd_from_focus,
    smear_kernel,
    subframe,
    surface_brightness_to_electrons,
    to_adu,
)
from .sky.wcs import fast_lst_deg, fast_radec_to_altaz, sensor_wcs
from .sources.base import RenderContext
from .wind import build_wind_model, path_to_pixels

log = logging.getLogger("astroskysim.rig")

SIDEREAL_DEG_S = 360.0 / 86164.0905


@dataclass
class MountState:
    ra_deg: float = 0.0
    dec_deg: float = 0.0
    target_ra_deg: float = 0.0
    target_dec_deg: float = 0.0
    slewing: bool = False
    tracking: bool = True
    parked: bool = False
    at_home: bool = False
    #: 0 = unknown, 1 = east (pointing west), 2 = west (pointing east)
    pier_side: int = 0
    #: Offset rates in arcsec/second, added on top of tracking.
    ra_rate: float = 0.0
    dec_rate: float = 0.0
    #: Manual jog, -1/0/+1 per axis, with slew_rate_index selecting the speed.
    jog_ra: int = 0
    jog_dec: int = 0
    slew_rate_index: int = 2
    #: Active guide pulses: remaining seconds per direction.
    guide_ns_s: float = 0.0
    guide_we_s: float = 0.0
    guide_ns_sign: int = 0
    guide_we_sign: int = 0
    #: Guide rate per axis, x sidereal. Seeded from the config, then owned by
    #: whatever the client writes to GUIDE_RATE.
    guide_rate_we: float = 0.5
    guide_rate_ns: float = 0.5
    park_ra_deg: float = 0.0
    park_dec_deg: float = 90.0
    #: Pointing-model correction learned from syncs, degrees in the reported
    #: frame: actual = reported - offset + error terms. See Rig.sync_to.
    sync_offset_ra: float = 0.0
    sync_offset_dec: float = 0.0
    #: 0 sidereal, 1 solar, 2 lunar, 3 custom
    track_rate_index: int = 0


@dataclass
class FocuserState:
    position: float = 15000.0
    target: float = 15000.0
    moving: bool = False
    temp_comp: bool = False
    reversed: bool = False
    backlash: int = 0
    #: Direction of the last completed move, for backlash bookkeeping.
    last_direction: int = 0
    #: Accumulated backlash debt not yet taken up.
    slack: float = 0.0


@dataclass
class RotatorState:
    angle: float = 0.0
    target: float = 0.0
    moving: bool = False
    reversed: bool = False


@dataclass
class FilterState:
    slot: int = 1
    target: int = 1
    remaining_s: float = 0.0

    @property
    def moving(self) -> bool:
        return self.remaining_s > 0.0


@dataclass
class CameraState:
    exposure_s: float = 1.0
    remaining_s: float = 0.0
    exposing: bool = False
    aborted: bool = False
    bin_x: int = 1
    bin_y: int = 1
    start_x: int = 0
    start_y: int = 0
    num_x: int = 0
    num_y: int = 0
    gain: int = 100
    offset: int = 100
    cooler_on: bool = False
    set_temperature: float = -10.0
    temperature: float = 20.0
    #: 0 light, 1 bias, 2 dark, 3 flat
    frame_type: int = 0
    #: Last completed frame, ADU, and the WCS the pixels were actually built on.
    last_frame: np.ndarray | None = None
    last_wcs: object | None = None
    last_exposure_s: float = -1.0
    last_start_time: str = ""
    #: Start of the current exposure as a JD. Satellite trails are integrated
    #: over the exposure *window*, and the readout runs in a thread minutes
    #: after the shutter opened on a long sub, so "now" is not the answer.
    start_jd: float = 0.0
    #: Satellites that reached the sensor in the last frame, for the header.
    last_satellites: int = 0
    #: Peak-to-peak wind smear of the last frame in *unbinned sensor* pixels -
    #: the smear runs before ``subframe`` and ``bin_frame``, so it is not in
    #: delivered pixels - and the wind that produced it. Ground truth for the
    #: header: a client inspecting a ruined sub has no other way to know whether
    #: it was wind.
    last_smear_px: float = 0.0
    last_wind_kmh: float = 0.0
    last_gust_kmh: float = 0.0
    #: Bumped whenever a new frame lands, so devices can detect staleness.
    sequence: int = 0


@dataclass
class Rig:
    cfg: Config
    mount: MountState = field(default_factory=MountState)
    focuser: FocuserState = field(default_factory=FocuserState)
    rotator: RotatorState = field(default_factory=RotatorState)
    filter: FilterState = field(default_factory=FilterState)
    camera: CameraState = field(default_factory=CameraState)
    guider: CameraState = field(default_factory=CameraState)
    #: Seconds since start, used for the periodic error phase and the clock.
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        c = self.cfg
        self.rng = np.random.default_rng(c.seed)
        # ``capture`` runs in a worker thread so a survey reprojection does not
        # freeze the server (see ``CameraBase._finish``). Both cameras draw from
        # the one ``rng``, which is not thread-safe, so only one capture runs at
        # a time. Serialising costs nothing: it is the *event loop* that has to
        # stay free, not the second camera.
        self.capture_lock = asyncio.Lock()
        self._start_jd = float(Time.now().jd)
        self.source = None  # injected by build_rig to avoid an import cycle
        #: ``SatelliteSky`` or None. None covers every ordinary reason there are
        #: no satellites - switched off, nothing fetched, extra not installed -
        #: so the imaging path only ever asks whether it is there.
        self.satellites = None
        #: ``WindModel`` or None when ``[wind]`` is off. Built here rather than
        #: injected by ``build_rig`` like ``source`` and ``satellites``, because
        #: ``wind`` imports only from ``config`` so there is no cycle to break -
        #: and a bare ``Rig(cfg)`` in a test then has weather like any other
        #: physics.
        self.wind = build_wind_model(c)

        self.focuser.position = float(c.focuser.perfect_focus)
        self.focuser.target = self.focuser.position
        self.focuser.backlash = c.focuser.backlash
        self.rotator.reversed = c.rotator.reversed
        self.mount.park_ra_deg = c.mount.park_ra_hours * 15.0
        self.mount.park_dec_deg = c.mount.park_dec_deg
        self.mount.guide_rate_we = c.mount.guide_rate
        self.mount.guide_rate_ns = c.mount.guide_rate
        self.mount.dec_deg = 45.0
        self.mount.target_dec_deg = 45.0
        self.camera.num_x = c.sensor.width_px
        self.camera.num_y = c.sensor.height_px
        self.guider.num_x = c.guide_sensor.width_px
        self.guider.num_y = c.guide_sensor.height_px

    # -- clock -------------------------------------------------------------
    # The tick runs at 10 Hz over six devices, so the clock is carried as a
    # plain float JD. Constructing an astropy Time per access (and then running
    # sidereal_time or an AltAz transform on it) costs hundreds of milliseconds
    # per tick and starves the loop.
    @property
    def jd(self) -> float:
        return self._start_jd + self.elapsed_s / 86400.0

    @property
    def now(self) -> Time:
        """astropy Time, for the rare places that need one (FITS DATE-OBS)."""
        return Time(self.jd, format="jd", scale="utc")

    @property
    def iso_utc(self) -> str:
        """ISO-8601 UTC without going through astropy formatting."""
        unix = (self.jd - 2440587.5) * 86400.0
        return (
            datetime.fromtimestamp(unix, tz=timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        )

    @property
    def lst_deg(self) -> float:
        return fast_lst_deg(self.jd, self.cfg.site.longitude)

    def altaz(self) -> tuple[float, float]:
        return fast_radec_to_altaz(
            self.mount.ra_deg, self.mount.dec_deg, self.cfg.site.latitude, self.lst_deg
        )

    # -- pointing ----------------------------------------------------------
    @property
    def actual_pointing(self) -> tuple[float, float]:
        """Where the optics really point, including all error terms.

        The starting point is the reported position minus the sync offset: a
        sync moves what the mount *claims* without moving the optics, so the
        correction it books has to come back off here.
        """
        return self.pointing_at()

    def pointing_at(self, wind_offset: tuple[float, float] | None = None):
        """``actual_pointing``, optionally with the wind term supplied.

        ``wind_offset`` is arcsec, ``(RA great-circle, Dec)``. ``None`` means
        "use the wind as it is right now", which is what every caller outside
        ``capture`` wants.

        ``capture`` supplies the *exposure window's mean* instead, and the
        distinction is not cosmetic. ``capture`` runs in the readout thread after
        the shutter closed - minutes later on a long sub - so the instantaneous
        offset it would otherwise read is a sample from outside the window
        entirely, while the smear kernel is zero-mean about the window mean. The
        frame would translate by the difference, which for a gust is the whole
        amplitude. Worse, the tick keeps stepping the wind during ``capture``, so
        the two would not even be reading the same value.
        """
        m = self.cfg.mount
        ra = self.mount.ra_deg - self.mount.sync_offset_ra
        dec = self.mount.dec_deg - self.mount.sync_offset_dec

        if m.periodic_error_amplitude > 0 and m.periodic_error_period > 0:
            phase = 2 * np.pi * self.elapsed_s / m.periodic_error_period
            ra += (m.periodic_error_amplitude * np.sin(phase)) / 3600.0 / max(
                np.cos(np.deg2rad(dec)), 1e-3
            )

        if m.tracking_noise > 0:
            ra += self.rng.normal(0, m.tracking_noise) / 3600.0 / max(
                np.cos(np.deg2rad(dec)), 1e-3
            )
            dec += self.rng.normal(0, m.tracking_noise) / 3600.0

        # Polar misalignment: a slow drift growing with time off the meridian.
        if m.azimuth_error or m.elevation_error:
            ha = np.deg2rad(self.lst_deg - ra)
            dec += (m.elevation_error / 60.0) * np.cos(ha)
            ra += (m.azimuth_error / 60.0) * np.sin(ha) / max(np.cos(np.deg2rad(dec)), 1e-3)

        # Wind. Deliberately in the same gap as the terms above: the guide camera
        # images actual_pointing, so a gust throws the guide star, the client
        # corrects it, and the pulse moves mount.ra_deg - shifting reported and
        # actual together. Sustained push therefore gets guided out with a lag
        # and the ring-down does not, which is the real behaviour and needs no
        # special case. Nothing here computes an RMS; the client's RMS is the
        # consequence.
        if wind_offset is None and self.wind is not None:
            wind_offset = self.wind.deflection
        if wind_offset is not None:
            d_ra, d_dec = wind_offset
            # The only cos(dec) in the feature: great-circle arcsec to RA
            # degrees, exactly as the three terms above do it. path_to_pixels
            # must not repeat it.
            ra += d_ra / 3600.0 / max(np.cos(np.deg2rad(dec)), 1e-3)
            dec += d_dec / 3600.0

        if not self.mount.tracking and not self.mount.slewing:
            # Tracking off: the sky drifts through the field.
            ra -= SIDEREAL_DEG_S * self._untracked_s

        return ra % 360.0, float(np.clip(dec, -90.0, 90.0))

    _untracked_s: float = 0.0

    def wind_time(self, jd: float) -> float:
        """Wind-model time for a JD, in seconds since the run started.

        ``jd = _start_jd + elapsed_s / 86400``, and the wind model's clock tracks
        ``elapsed_s``, so this inverts the one to index the other. Keeps the
        exposure window off ``CameraState``, which already records the start as a
        JD for the satellite trails.
        """
        return (jd - self._start_jd) * 86400.0

    # -- simulation --------------------------------------------------------
    async def step(self, dt: float) -> None:
        self.elapsed_s += dt
        if self.wind is not None:
            self.wind.step(dt)
        self._step_mount(dt)
        self._step_focuser(dt)
        self._step_rotator(dt)
        self._step_filter(dt)
        self._step_temperature(dt)

    def _step_mount(self, dt: float) -> None:
        m, cfg = self.mount, self.cfg.mount
        self._untracked_s = 0.0 if m.tracking else self._untracked_s + dt

        if m.slewing:
            step = cfg.slew_rate_deg_s * dt
            dra = _wrap180(m.target_ra_deg - m.ra_deg)
            ddec = m.target_dec_deg - m.dec_deg
            dist = float(np.hypot(dra * np.cos(np.deg2rad(m.dec_deg)), ddec))
            if dist <= step or dist < 1e-6:
                m.ra_deg, m.dec_deg = m.target_ra_deg % 360.0, m.target_dec_deg
                m.slewing = False
                m.at_home = False
                self._update_pier_side()
            else:
                f = step / dist
                m.ra_deg = (m.ra_deg + dra * f) % 360.0
                m.dec_deg += ddec * f
            return

        # Jog overrides guiding; both are relative to the tracked position.
        rate = self._jog_rate_deg_s()
        if m.jog_ra or m.jog_dec:
            m.ra_deg = (m.ra_deg + m.jog_ra * rate * dt / max(np.cos(np.deg2rad(m.dec_deg)), 1e-3)) % 360.0
            m.dec_deg = float(np.clip(m.dec_deg + m.jog_dec * rate * dt, -90, 90))

        if m.guide_we_s > 0:
            used = min(dt, m.guide_we_s)
            rate = m.guide_rate_we * SIDEREAL_DEG_S
            m.ra_deg = (m.ra_deg + m.guide_we_sign * rate * used) % 360.0
            m.guide_we_s -= used
        if m.guide_ns_s > 0:
            used = min(dt, m.guide_ns_s)
            rate = m.guide_rate_ns * SIDEREAL_DEG_S
            m.dec_deg = float(np.clip(m.dec_deg + m.guide_ns_sign * rate * used, -90, 90))
            m.guide_ns_s -= used

        if m.ra_rate or m.dec_rate:
            m.ra_deg = (m.ra_deg + m.ra_rate * dt / 3600.0) % 360.0
            m.dec_deg = float(np.clip(m.dec_deg + m.dec_rate * dt / 3600.0, -90, 90))

    def _jog_rate_deg_s(self) -> float:
        """Slew-rate index 0..3 maps to guide, centre, find and max speeds."""
        table = (
            self.mount.guide_rate_we * SIDEREAL_DEG_S,
            8 * SIDEREAL_DEG_S,
            0.5,
            self.cfg.mount.slew_rate_deg_s,
        )
        return table[max(0, min(self.mount.slew_rate_index, len(table) - 1))]

    def _update_pier_side(self) -> None:
        ha = _wrap180(self.lst_deg - self.mount.ra_deg)
        self.mount.pier_side = 2 if ha < 0 else 1

    def _step_focuser(self, dt: float) -> None:
        f = self.focuser
        if not f.moving:
            return
        step = self.cfg.focuser.speed_steps_s * dt
        delta = f.target - f.position
        direction = 1 if delta > 0 else -1

        # Backlash: a reversal spends `backlash` steps taking up slack before
        # the optics move at all. Exposed as FOCUS_BACKLASH so a client can see
        # the value it is expected to compensate for.
        if f.backlash and direction != f.last_direction and f.last_direction != 0:
            f.slack = float(f.backlash)
            f.last_direction = direction
        if f.slack > 0:
            taken = min(step, f.slack)
            f.slack -= taken
            step -= taken
            if step <= 0:
                return

        if abs(delta) <= step:
            f.position = f.target
            f.moving = False
        else:
            f.position += direction * step
        f.last_direction = direction

    def _step_rotator(self, dt: float) -> None:
        r = self.rotator
        if not r.moving:
            return
        step = self.cfg.rotator.speed_deg_s * dt
        delta = _wrap180(r.target - r.angle)
        if abs(delta) <= step:
            r.angle = r.target % 360.0
            r.moving = False
        else:
            r.angle = (r.angle + np.sign(delta) * step) % 360.0

    def _step_filter(self, dt: float) -> None:
        if self.filter.remaining_s > 0:
            self.filter.remaining_s = max(0.0, self.filter.remaining_s - dt)
            if self.filter.remaining_s == 0:
                self.filter.slot = self.filter.target

    def _step_temperature(self, dt: float) -> None:
        for cam in (self.camera, self.guider):
            target = cam.set_temperature if cam.cooler_on else 20.0
            # First-order approach, ~30 s time constant.
            cam.temperature += (target - cam.temperature) * min(dt / 30.0, 1.0)

    # -- commands ----------------------------------------------------------
    def slew_to(self, ra_deg: float, dec_deg: float) -> None:
        if self.mount.parked:
            raise ValueError("mount is parked")
        self.mount.target_ra_deg = ra_deg % 360.0
        self.mount.target_dec_deg = float(np.clip(dec_deg, -90, 90))
        self.mount.slewing = True
        self.mount.tracking = True

    def sync_to(self, ra_deg: float, dec_deg: float) -> None:
        """Tell the mount it is pointing at these coordinates.

        A sync must not move the optics. It moves the *reported* position onto
        the coordinates the client asserts and books the difference as a
        pointing-model correction, so the next slew lands the *actual* pointing
        on the target.

        Overwriting ``ra_deg``/``dec_deg`` alone dragged the actual pointing
        along with the reported one, which left the plate-solve error exactly
        unchanged through every sync-and-slew iteration: Ekos' "Slew to target"
        moved the mount by the residual and then measured the same residual
        again, forever.
        """
        m = self.mount
        ra_deg %= 360.0
        dec_deg = float(np.clip(dec_deg, -90, 90))
        m.sync_offset_ra = _wrap180(m.sync_offset_ra + ra_deg - m.ra_deg)
        m.sync_offset_dec += dec_deg - m.dec_deg
        m.ra_deg, m.dec_deg = ra_deg, dec_deg
        m.target_ra_deg, m.target_dec_deg = ra_deg, dec_deg
        self._update_pier_side()
        log.debug(
            "sync: reported %.5f/%.5f, pointing model now %+.1f\"/%+.1f\"",
            ra_deg,
            dec_deg,
            m.sync_offset_ra * 3600.0,
            m.sync_offset_dec * 3600.0,
        )

    def abort_slew(self) -> None:
        self.mount.slewing = False
        self.mount.jog_ra = self.mount.jog_dec = 0
        self.mount.target_ra_deg = self.mount.ra_deg
        self.mount.target_dec_deg = self.mount.dec_deg

    def move_focuser(self, target: float) -> None:
        self.focuser.target = float(np.clip(target, 0, self.cfg.focuser.max_position))
        self.focuser.moving = True

    def move_rotator(self, angle: float) -> None:
        self.rotator.target = angle % 360.0
        self.rotator.moving = True

    def select_filter(self, slot: int) -> None:
        n = len(self.cfg.filter_wheel.names)
        slot = max(1, min(slot, n))
        if slot != self.filter.slot:
            self.filter.target = slot
            self.filter.remaining_s = self.cfg.filter_wheel.change_time_s

    # -- imaging -----------------------------------------------------------
    @property
    def sky_position_angle(self) -> float:
        """Rotator sky angle, honouring reverse and the mechanical offset."""
        a = self.rotator.angle
        if self.rotator.reversed:
            a = -a
        return (a + self.cfg.rotator.mechanical_offset) % 360.0

    def current_hfd(self) -> float:
        """HFD in pixels at the current focus, including the filter offset."""
        offsets = self.cfg.filter_wheel.focus_offsets
        idx = max(0, min(self.filter.slot - 1, len(offsets) - 1))
        perfect = self.cfg.focuser.perfect_focus + offsets[idx]
        return hfd_from_focus(self.focuser.position, perfect, self.cfg.focuser.focus_range)

    def guide_hfd(self) -> float:
        """HFD in pixels on the guide camera.

        Default is an off-axis guider, whose pickoff prism sits in the imaging
        train *downstream of the focuser*: a focus move defocuses the guide star
        along with the imaging one, which is exactly why Ekos suspends guiding
        during an autofocus run.

        The prism is *upstream of the filter wheel*, though - it has to be, or a
        narrowband filter would starve the guide camera. So the per-filter focus
        offset that brings the imaging chip into focus moves the guide star the
        same distance *out* of it, and the guide star goes slightly soft on Ha.
        Hence no filter offset here, unlike ``current_hfd``.

        A separate guide scope instead carries its own fixed focus, untouched by
        the imaging focuser: set ``optics.guide_hfd_px`` for that rig.
        """
        fixed = self.cfg.optics.guide_hfd_px
        if fixed is not None:
            return fixed
        return hfd_from_focus(
            self.focuser.position,
            self.cfg.focuser.perfect_focus,
            self.cfg.focuser.focus_range,
        )

    # -- per-camera geometry ----------------------------------------------
    # The two cameras are different hardware: their own chip, their own plate
    # scale and, with a guide scope configured, their own aperture. Everything
    # downstream has to ask which camera it is holding.
    def is_guider(self, cam: CameraState) -> bool:
        return cam is self.guider

    def sensor_cfg(self, cam: CameraState):
        """The configured sensor spec behind this camera."""
        return self.cfg.guide_sensor if self.is_guider(cam) else self.cfg.sensor

    def scale_arcsec_px(self, cam: CameraState) -> float:
        return self.cfg.guide_scale_arcsec_px if self.is_guider(cam) else self.cfg.scale_arcsec_px

    def sensor_model(self, cam: CameraState) -> SensorModel:
        """Detector model for this camera, at its current gain and offset."""
        s = self.sensor_cfg(cam)
        return SensorModel(
            well_depth_e=s.well_depth_e,
            read_noise_e=s.read_noise_e,
            e_per_adu=s.e_per_adu,
            bit_depth=s.bit_depth,
            hot_pixels=s.hot_pixels,
            hot_pixel_e_s=s.hot_pixel_e_s,
            gain=cam.gain,
            offset_adu=cam.offset,
        )

    def filter_transmission(self) -> float:
        """Transmission of the filter currently in the imaging beam."""
        return self.cfg.filter_wheel.transmission_for(self.filter.slot)

    def filter_name(self) -> str:
        """Name of the filter currently in the imaging beam."""
        names = self.cfg.filter_wheel.names
        return names[max(0, min(self.filter.slot - 1, len(names) - 1))]

    def build_optics(self, cam: CameraState | None = None) -> Optics:
        tel = self.cfg.telescope
        opt = self.cfg.optics
        if cam is not None and self.is_guider(cam):
            # No filter term: the pickoff prism is upstream of the filter
            # wheel, for the same reason ``guide_hfd`` drops the focus offset -
            # a narrowband filter in the guide beam would starve the guider.
            return Optics(
                aperture_mm=tel.guide_aperture,
                scale_arcsec_px=self.cfg.guide_scale_arcsec_px,
                seeing_arcsec=opt.seeing_arcsec,
                hfd_px=self.guide_hfd(),
                throughput=opt.throughput,
                zero_point=opt.zero_point_e_s_m2,
            )
        return Optics(
            aperture_mm=tel.aperture_mm,
            scale_arcsec_px=self.cfg.scale_arcsec_px,
            seeing_arcsec=opt.seeing_arcsec,
            hfd_px=self.current_hfd(),
            # Folding the filter into the throughput attenuates stars, survey
            # nebulosity and sky in one place, which is what a filter does.
            throughput=opt.throughput * self.filter_transmission(),
            zero_point=opt.zero_point_e_s_m2,
        )

    def sky_e_s(self, cam: CameraState) -> float:
        """Sky background in e-/px/s for this camera, at its own plate scale.

        Derived from the configured SQM through the same zero point the stars
        use, so a smaller pixel or a narrower filter darkens the background the
        way it does on the sky. ``optics.sky_background`` short-circuits this
        with a raw electron rate for anyone who wants to dial one in.
        """
        override = self.cfg.optics.sky_background
        if override is not None:
            return override
        return surface_brightness_to_electrons(
            self.cfg.optics.sky_mag_arcsec2, self.build_optics(cam)
        )

    def equivalent_sqm(self, e_px_s: float) -> float:
        """The SQM reading a raw e-/px/s rate corresponds to on this rig.

        Only used to make the ``sky_background`` override's unit legible in the
        startup log.
        """
        unit = surface_brightness_to_electrons(0.0, self.build_optics(self.camera))
        if e_px_s <= 0 or unit <= 0:
            return float("inf")
        return float(-2.5 * np.log10(e_px_s / unit))

    def build_wcs(
        self,
        width: int,
        height: int,
        scale_arcsec_px: float | None = None,
        wind_offset: tuple[float, float] | None = None,
    ):
        ra, dec = self.pointing_at(wind_offset)
        return sensor_wcs(
            ra,
            dec,
            width,
            height,
            self.cfg.scale_arcsec_px if scale_arcsec_px is None else scale_arcsec_px,
            position_angle_deg=self.sky_position_angle,
        )

    def add_satellite_trails(
        self, cam: CameraState, electrons: np.ndarray, wcs, optics: Optics
    ) -> np.ndarray:
        """Satellite trails over an already-rendered frame.

        Deliberately not an ``ImageSource``. A source answers "what is on the
        sky in this direction" and reprojects a static scene; a trail is an
        integration over *when the shutter was open*, which is not in a
        ``RenderContext`` and should not have to be. Adding it here also keeps
        it clear of the composite path's point-source suppression, which would
        otherwise erase exactly the thing it draws.

        Light frames only: a satellite cannot reach a bias, a dark or a flat,
        and a trail in a calibration frame would be a bug a client cannot
        distinguish from a real one.
        """
        cam.last_satellites = 0
        if self.satellites is None or cam.frame_type != 0 or cam.exposure_s <= 0:
            return electrons
        # The readout runs in a thread and can start minutes after the shutter
        # opened, so the window is anchored on the recorded start, not on now.
        start_jd = cam.start_jd or (self.jd - cam.exposure_s / 86400.0)
        try:
            trails, drawn = self.satellites.render(
                wcs=wcs,
                shape=electrons.shape,
                optics=optics,
                exposure_s=cam.exposure_s,
                jd_end=start_jd + cam.exposure_s / 86400.0,
            )
        except Exception:
            # A frame is worth more than a trail: the same reasoning as the
            # composite background falling back to stars only.
            log.exception("satellite trails failed; frame delivered without them")
            return electrons
        cam.last_satellites = len(drawn)
        return electrons + trails

    def exposure_window(self, cam: CameraState):
        """The wind's deflection over this exposure, or None.

        Anchored on ``cam.start_jd`` rather than on now, for the reason
        ``add_satellite_trails`` gives: the readout runs in a thread and can
        begin minutes after the shutter opened.
        """
        if self.wind is None or cam.exposure_s <= 0 or cam.frame_type != 0:
            return None
        start_jd = cam.start_jd or (self.jd - cam.exposure_s / 86400.0)
        return self.wind.window(self.wind_time(start_jd), cam.exposure_s)

    def apply_wind_smear(self, cam: CameraState, electrons: np.ndarray, wcs, window):
        """Smear an already-rendered frame along the wind's path.

        One kernel for the whole frame, which is exactly right for a
        translation: wind deflection moves every star in the field together, so
        stars, survey nebulosity and any satellite trail all smear as one. That
        also makes this the wrong place for anything position-dependent - field
        rotation and differential flexure are not modelled here.

        Placement in ``capture`` is load-bearing in three directions. Before
        ``apply_bayer``, because a real CFA samples a smeared *scene* rather than
        smearing an already-attenuated mosaic. Before ``add_sky_and_noise``,
        because convolving read noise would correlate it and leave the frame
        smoother than the sensor is. After ``add_satellite_trails``, because a
        trail shakes with the tube too - and because it puts this downstream of
        the composite path's ``suppress_point_sources``, which would otherwise
        erase the streak.

        Light frames only, like the trails: a wind smear on a flat is a no-op
        except at the border, and on a bias there is nothing to smear.
        """
        cam.last_smear_px = 0.0
        if window is None or len(window) < 2:
            return electrons
        dx, dy = path_to_pixels(wcs, window.d_ra, window.d_dec)
        kernel = smear_kernel(dx, dy)
        if kernel is None:
            return electrons
        cam.last_smear_px = float(max(np.ptp(dx), np.ptp(dy)))
        return apply_smear(electrons, kernel)

    def capture(self, cam: CameraState) -> np.ndarray:
        """Produce one frame in ADU, honouring binning, subframe and type."""
        s = self.sensor_cfg(cam)
        # The window is taken *once* and handed to both consumers. The WCS gets
        # its mean, the kernel gets the zero-mean path about that mean, so the
        # smear spreads a star without translating it and a plate solve of a
        # wind-ruined sub still returns the true centre. Re-reading the wind for
        # either one reintroduces both a bias and a race - see pointing_at.
        window = self.exposure_window(cam)
        full_wcs = self.build_wcs(
            s.width_px,
            s.height_px,
            self.scale_arcsec_px(cam),
            wind_offset=None if window is None else window.mean,
        )
        optics = self.build_optics(cam)
        sky_e_s = self.sky_e_s(cam)
        # Every read of actual_pointing redraws the tracking noise, so the
        # header has to reuse *this* WCS rather than build a second one that
        # disagrees with the pixels by the noise amplitude.
        cam.last_wcs = full_wcs
        if self.wind is not None:
            cam.last_wind_kmh = self.wind.speed_kmh
            cam.last_gust_kmh = self.wind.reported_gust_kmh

        dark = cam.frame_type in (1, 2)  # bias or dark
        if dark or self.source is None:
            electrons = np.zeros((s.height_px, s.width_px), dtype=np.float64)
        else:
            # The guide camera sees no filter at all - its pickoff prism is
            # upstream of the wheel, the same reason ``guide_hfd`` drops the
            # focus offset. So it never picks up a per-filter survey, and a
            # narrowband layer can never starve the guider.
            guiding = self.is_guider(cam)
            ctx = RenderContext(
                wcs=full_wcs,
                shape=(s.height_px, s.width_px),
                optics=optics,
                exposure_s=0.0 if cam.frame_type == 1 else cam.exposure_s,
                rng=self.rng,
                sky_e_s=sky_e_s,
                filter_name=None if guiding else self.filter_name(),
                filter_transmission=1.0 if guiding else self.filter_transmission(),
            )
            electrons = self.source.render(ctx)
            if cam.frame_type == 3:  # flat: uniform illumination, no stars
                electrons = np.full_like(electrons, 0.4 * s.well_depth_e)

        electrons = self.add_satellite_trails(cam, electrons, full_wcs, optics)
        electrons = self.apply_wind_smear(cam, electrons, full_wcs, window)
        electrons = apply_bayer(electrons, s.bayer)
        sensor = self.sensor_model(cam)
        electrons = add_sky_and_noise(
            electrons,
            sensor,
            0.0 if cam.frame_type == 1 else cam.exposure_s,
            sky_e_s,
            self.rng,
            dark_frame=dark,
        )
        electrons = add_hot_pixels(
            electrons,
            s.hot_pixels,
            sensor,
            self.cfg.seed,
            0.0 if cam.frame_type == 1 else cam.exposure_s,
        )

        electrons = subframe(
            electrons,
            cam.start_x,
            cam.start_y,
            cam.num_x or s.width_px,
            cam.num_y or s.height_px,
        )
        electrons = bin_frame(electrons, cam.bin_x, cam.bin_y)
        return to_adu(electrons, sensor)


def _wrap180(deg: float) -> float:
    """Shortest signed angular difference, in degrees."""
    return float((deg + 180.0) % 360.0 - 180.0)


def build_rig(cfg: Config) -> Rig:
    from .satellites.config import load_satellites_config
    from .satellites.trails import build_satellite_sky
    from .sources.registry import build_source

    rig = Rig(cfg)
    rig.source = build_source(cfg)
    rig.satellites = build_satellite_sky(
        load_satellites_config(cfg.satellites), cfg.site, rig.jd
    )
    g = cfg.guide_sensor
    log.info(
        "rig ready: imaging %dx%d px at %.2f\"/px, guider %dx%d px at %.2f\"/px "
        "(%s), source=%s",
        cfg.sensor.width_px,
        cfg.sensor.height_px,
        cfg.scale_arcsec_px,
        g.width_px,
        g.height_px,
        cfg.guide_scale_arcsec_px,
        f"guide scope {cfg.telescope.guide_focal_length:.0f} mm"
        if cfg.telescope.separate_guide_scope
        else "off-axis, main OTA",
        getattr(rig.source, "name", "?"),
    )
    if cfg.sensor_guide_cam is None:
        log.warning(
            "no [sensor_guide_cam] section: the guide camera reports the imaging "
            "sensor's specs, which no real rig does"
        )
    if rig.wind is not None:
        w = cfg.wind
        # The arcsec-to-pixel step is the whole unit trap in this section, so it
        # is logged rather than left to be discovered in a ruined sub.
        sustained = w.response_arcsec_at_20kmh * (w.speed_kmh / 20.0) ** 2
        gust = w.response_arcsec_at_20kmh * (w.gust_speed_kmh / 20.0) ** 2
        log.info(
            "wind on: %.0f km/h sustained (%.2f\" = %.1f px imaging, %.1f px guiding), "
            "gusts to %.0f km/h (%.2f\" = %.1f px), ringing at %.1f Hz zeta %.2f",
            w.speed_kmh,
            sustained,
            sustained / cfg.scale_arcsec_px,
            sustained / cfg.guide_scale_arcsec_px,
            w.gust_speed_kmh,
            gust,
            gust / cfg.scale_arcsec_px,
            w.resonance_hz,
            w.damping,
        )
        if not cfg.server.weather:
            log.info(
                "no weather device is advertised, so no client can see the wind or "
                "react to it; set server.weather = true to expose it"
            )
    if cfg.optics.sky_background is not None:
        # The unit is the trap: a value near 21 reads as an SQM figure and is
        # in fact about fifty times the electron rate an SQM 21 sky produces.
        log.warning(
            "optics.sky_background = %.3g overrides the sky model with a raw "
            "e-/px/s rate (equivalent to SQM %.1f here); use "
            "optics.sky_mag_arcsec2 for an SQM reading",
            cfg.optics.sky_background,
            rig.equivalent_sqm(cfg.optics.sky_background),
        )
    else:
        log.info(
            "sky %.1f mag/arcsec2 -> %.3f e-/px/s imaging, %.3f e-/px/s guiding",
            cfg.optics.sky_mag_arcsec2,
            rig.sky_e_s(rig.camera),
            rig.sky_e_s(rig.guider),
        )
    return rig
