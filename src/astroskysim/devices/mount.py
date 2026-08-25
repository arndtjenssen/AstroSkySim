"""Telescope mount.

The property set is deliberately wide. A mount that only answers
EQUATORIAL_EOD_COORD lets a client test almost nothing, so anything the rig
already simulates is exposed rather than kept internal.
"""

from __future__ import annotations

from ..indi.device import GUIDER_INTERFACE, TELESCOPE_INTERFACE, Device
from ..indi.protocol import (
    NumberItem,
    NumberVector,
    Perm,
    PropState,
    SwitchItem,
    SwitchRule,
    SwitchVector,
    TextItem,
    TextVector,
    Vector,
    parse_number,
)
from ..sky.wcs import fast_altaz_to_radec
from .pulse import GuidePulseMixin


class Mount(GuidePulseMixin, Device):
    device_name = "AstroSkySim Telescope"
    # GUIDER is the ST4 bit: it is what tells a client this mount can be pulsed
    # during guider calibration. See devices/pulse.py.
    interface = TELESCOPE_INTERFACE | GUIDER_INTERFACE

    def setup(self) -> None:  # noqa: PLR0915 - one flat block of declarations
        cfg = self.rig.cfg
        m = self.rig.mount

        self.eq = self.add(
            NumberVector(
                name="EQUATORIAL_EOD_COORD",
                label="Eq. Coordinates",
                items=[
                    NumberItem("RA", "RA (hh:mm:ss)", m.ra_deg / 15.0, "%010.6m", 0, 24, 0),
                    NumberItem("DEC", "DEC (dd:mm:ss)", m.dec_deg, "%010.6m", -90, 90, 0),
                ],
            )
        )
        self.on_coord_set = self.add(
            SwitchVector(
                name="ON_COORD_SET",
                label="On Set",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("TRACK", "Track", True),
                    SwitchItem("SLEW", "Slew", False),
                    SwitchItem("SYNC", "Sync", False),
                ],
            )
        )
        self.abort = self.add(
            SwitchVector(
                name="TELESCOPE_ABORT_MOTION",
                label="Abort Motion",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("ABORT", "Abort")],
            )
        )
        self.track_state = self.add(
            SwitchVector(
                name="TELESCOPE_TRACK_STATE",
                label="Tracking",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("TRACK_ON", "On", m.tracking),
                    SwitchItem("TRACK_OFF", "Off", not m.tracking),
                ],
            )
        )
        # Non-sidereal rates, so lunar and solar tracking are testable.
        self.track_rate = self.add(
            SwitchVector(
                name="TELESCOPE_TRACK_RATE",
                label="Track Rate",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("TRACK_SIDEREAL", "Sidereal", True),
                    SwitchItem("TRACK_SOLAR", "Solar"),
                    SwitchItem("TRACK_LUNAR", "Lunar"),
                    SwitchItem("TRACK_CUSTOM", "Custom"),
                ],
            )
        )
        # Manual jog, the hand-controller equivalent.
        self.motion_ns = self.add(
            SwitchVector(
                name="TELESCOPE_MOTION_NS",
                label="Motion N/S",
                group="Motion Control",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("MOTION_NORTH", "North"), SwitchItem("MOTION_SOUTH", "South")],
            )
        )
        self.motion_we = self.add(
            SwitchVector(
                name="TELESCOPE_MOTION_WE",
                label="Motion W/E",
                group="Motion Control",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("MOTION_WEST", "West"), SwitchItem("MOTION_EAST", "East")],
            )
        )
        # Jog speed for the motion switches above.
        self.slew_rate = self.add(
            SwitchVector(
                name="TELESCOPE_SLEW_RATE",
                label="Slew Rate",
                group="Motion Control",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("SLEW_GUIDE", "Guide"),
                    SwitchItem("SLEW_CENTERING", "Centering"),
                    SwitchItem("SLEW_FIND", "Find", True),
                    SwitchItem("SLEW_MAX", "Max"),
                ],
            )
        )
        self.add_guide_pulse_properties()
        # Pulse-guide rate as a fraction of sidereal. A guiding client reads
        # this to turn a measured drift into a pulse duration.
        self.guide_rate = self.add(
            NumberVector(
                name="GUIDE_RATE",
                label="Guide Rate",
                group="Motion Control",
                items=[
                    NumberItem(
                        "GUIDE_RATE_WE", "W/E (x sidereal)",
                        cfg.mount.guide_rate, "%.2f", 0.05, 1.0, 0.05,
                    ),
                    NumberItem(
                        "GUIDE_RATE_NS", "N/S (x sidereal)",
                        cfg.mount.guide_rate, "%.2f", 0.05, 1.0, 0.05,
                    ),
                ],
            )
        )
        # Offset tracking rates, for a comet or minor planet. INDI has no
        # standard property for these, so this is a documented extension.
        self.offset_rates = self.add(
            NumberVector(
                name="TELESCOPE_OFFSET_RATES",
                label="Offset Rates",
                group="Motion Control",
                items=[
                    NumberItem("OFFSET_RATE_RA", "RA (arcsec/s)", 0.0, "%.4f", -60, 60, 0.001),
                    NumberItem("OFFSET_RATE_DEC", "DEC (arcsec/s)", 0.0, "%.4f", -60, 60, 0.001),
                ],
            )
        )
        # The rig computes alt/az anyway, so report it and accept slews in it.
        self.horiz = self.add(
            NumberVector(
                name="HORIZONTAL_COORD",
                label="Horiz. Coordinates",
                items=[
                    NumberItem("ALT", "Alt (dd:mm:ss)", 0, "%010.6m", -90, 90, 0),
                    NumberItem("AZ", "Az (dd:mm:ss)", 0, "%010.6m", 0, 360, 0),
                ],
            )
        )
        self.park = self.add(
            SwitchVector(
                name="TELESCOPE_PARK",
                label="Park",
                group="Site Management",
                rule=SwitchRule.ONE_OF_MANY,
                items=[SwitchItem("PARK", "Park"), SwitchItem("UNPARK", "Unpark", True)],
            )
        )
        # A settable park position, so park/unpark can be tested against a
        # known spot rather than a hardcoded one.
        self.park_position = self.add(
            NumberVector(
                name="TELESCOPE_PARK_POSITION",
                label="Park Position",
                group="Site Management",
                items=[
                    NumberItem("PARK_RA", "RA (hh:mm:ss)", m.park_ra_deg / 15.0, "%010.6m", 0, 24, 0),
                    NumberItem("PARK_DEC", "DEC (dd:mm:ss)", m.park_dec_deg, "%010.6m", -90, 90, 0),
                ],
            )
        )
        self.park_option = self.add(
            SwitchVector(
                name="TELESCOPE_PARK_OPTION",
                label="Park Options",
                group="Site Management",
                rule=SwitchRule.AT_MOST_ONE,
                items=[
                    SwitchItem("PARK_CURRENT", "Current"),
                    SwitchItem("PARK_DEFAULT", "Default"),
                    SwitchItem("PARK_WRITE_DATA", "Write"),
                ],
            )
        )
        # Find-home, distinct from park: home is a fixed mechanical reference.
        self.home = self.add(
            SwitchVector(
                name="TELESCOPE_HOME",
                label="Homing",
                group="Site Management",
                rule=SwitchRule.AT_MOST_ONE,
                items=[
                    SwitchItem("FIND_HOME", "Find"),
                    SwitchItem("SET_HOME", "Set"),
                    SwitchItem("GO_HOME", "Go"),
                ],
            )
        )
        self.pier_side = self.add(
            SwitchVector(
                name="TELESCOPE_PIER_SIDE",
                label="Pier Side",
                perm=Perm.RO,
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("PIER_EAST", "East"), SwitchItem("PIER_WEST", "West")],
            )
        )
        self.geo = self.add(
            NumberVector(
                name="GEOGRAPHIC_COORD",
                label="Site",
                group="Site Management",
                items=[
                    NumberItem("LAT", "Lat (dd:mm:ss)", cfg.site.latitude, "%010.6m", -90, 90, 0),
                    NumberItem("LONG", "Lon (dd:mm:ss)", cfg.site.longitude % 360, "%010.6m", 0, 360, 0),
                    NumberItem("ELEV", "Elevation (m)", cfg.site.elevation, "%.1f", -200, 10000, 1),
                ],
            )
        )
        self.time_utc = self.add(
            TextVector(
                name="TIME_UTC",
                label="UTC",
                group="Site Management",
                items=[TextItem("UTC", "UTC Time", ""), TextItem("OFFSET", "UTC Offset", "0")],
            )
        )
        # LST, which the rig computes for the WCS anyway.
        self.time_lst = self.add(
            NumberVector(
                name="TIME_LST",
                label="Local Sidereal Time",
                group="Site Management",
                perm=Perm.RO,
                items=[NumberItem("LST", "LST (hh:mm:ss)", 0, "%010.6m", 0, 24, 0)],
            )
        )
        self.info = self.add(
            NumberVector(
                name="TELESCOPE_INFO",
                label="Scope Properties",
                group="Options",
                items=[
                    NumberItem(
                        "TELESCOPE_APERTURE", "Aperture (mm)",
                        cfg.telescope.aperture_mm, "%.1f", 10, 5000, 1,
                    ),
                    NumberItem(
                        "TELESCOPE_FOCAL_LENGTH", "Focal Length (mm)",
                        cfg.telescope.focal_length_mm, "%.1f", 10, 20000, 1,
                    ),
                    # The guide scope, which is only the imaging OTA when the
                    # guider is off-axis. Ekos reads these to size its guide
                    # star search box and to scale calibration.
                    NumberItem(
                        "GUIDER_APERTURE", "Guider Aperture (mm)",
                        cfg.telescope.guide_aperture, "%.1f", 10, 5000, 1,
                    ),
                    NumberItem(
                        "GUIDER_FOCAL_LENGTH", "Guider Focal Length (mm)",
                        cfg.telescope.guide_focal_length, "%.1f", 10, 20000, 1,
                    ),
                ],
            )
        )

        for name, fn in (
            ("EQUATORIAL_EOD_COORD", self._w_eq),
            ("HORIZONTAL_COORD", self._w_horiz),
            ("ON_COORD_SET", self._w_simple_switch),
            ("TELESCOPE_ABORT_MOTION", self._w_abort),
            ("TELESCOPE_TRACK_STATE", self._w_track_state),
            ("TELESCOPE_TRACK_RATE", self._w_track_rate),
            ("TELESCOPE_MOTION_NS", self._w_motion_ns),
            ("TELESCOPE_MOTION_WE", self._w_motion_we),
            ("TELESCOPE_SLEW_RATE", self._w_slew_rate),
            ("GUIDE_RATE", self._w_guide_rate),
            ("TELESCOPE_OFFSET_RATES", self._w_offset_rates),
            ("TELESCOPE_PARK", self._w_park),
            ("TELESCOPE_PARK_POSITION", self._w_park_position),
            ("TELESCOPE_PARK_OPTION", self._w_park_option),
            ("TELESCOPE_HOME", self._w_home),
            ("GEOGRAPHIC_COORD", self._w_geo),
            ("TIME_UTC", self._w_time),
            ("TELESCOPE_INFO", self._w_info),
        ):
            self.writer(name, fn)

    # -- writes ------------------------------------------------------------
    async def _w_simple_switch(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)

    async def _w_eq(self, vec: Vector, values: dict[str, str]) -> None:
        ra_h = parse_number(values.get("RA", str(self.rig.mount.ra_deg / 15.0)))
        dec = parse_number(values.get("DEC", str(self.rig.mount.dec_deg)))
        ra_deg = ra_h * 15.0
        action = self.on_coord_set.selected or "TRACK"

        vec["RA"].value = ra_h
        vec["DEC"].value = dec

        if action == "SYNC":
            self.rig.sync_to(ra_deg, dec)
            self.push(vec, state=PropState.OK)
            self._push_pier_side()
            return
        try:
            self.rig.slew_to(ra_deg, dec)
        except ValueError as exc:
            self.push(vec, state=PropState.ALERT, message=str(exc))
            return
        # SLEW means goto then stop tracking; TRACK means keep tracking.
        self._stop_tracking_after_slew = action == "SLEW"
        self.push(vec, state=PropState.BUSY)

    _stop_tracking_after_slew = False

    async def _w_horiz(self, vec: Vector, values: dict[str, str]) -> None:
        alt = parse_number(values.get("ALT", "0"))
        az = parse_number(values.get("AZ", "0"))
        cfg = self.rig.cfg.site
        ra, dec = fast_altaz_to_radec(az, alt, cfg.latitude, self.rig.lst_deg)
        vec["ALT"].value = alt
        vec["AZ"].value = az
        try:
            self.rig.slew_to(ra, dec)
        except ValueError as exc:
            self.push(vec, state=PropState.ALERT, message=str(exc))
            return
        self.push(vec, state=PropState.BUSY)

    async def _w_abort(self, vec: Vector, values: dict[str, str]) -> None:
        self.rig.abort_slew()
        for it in vec.items:
            it.value = False
        for v in (self.motion_ns, self.motion_we):
            for it in v.items:
                it.value = False
            self.push(v, state=PropState.IDLE)
        self.push(vec, state=PropState.OK, message="motion aborted")
        # An abort stops the *slew*; a tracking mount is still tracking, and a
        # flat Idle here reads as "stopped" until the next tick corrects it.
        self.push(self.eq, state=self._eq_state())

    async def _w_track_state(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        wanted = vec.selected == "TRACK_ON"  # type: ignore[attr-defined]
        if wanted and self.rig.mount.parked:
            # Roll the switch back, or the client keeps showing the On it asked
            # for. Coalescing merges this into the one alert below.
            vec["TRACK_ON"].value = False
            vec["TRACK_OFF"].value = True
            self.push(vec, state=PropState.ALERT, message="mount is parked")
            return
        self.rig.mount.tracking = wanted
        self.push(vec, state=PropState.OK)
        # The status a client displays comes from EQUATORIAL_EOD_COORD, so Off
        # is not visible until that goes out too.
        self.push(self.eq, state=self._eq_state())

    async def _w_track_rate(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        order = ["TRACK_SIDEREAL", "TRACK_SOLAR", "TRACK_LUNAR", "TRACK_CUSTOM"]
        sel = vec.selected  # type: ignore[attr-defined]
        self.rig.mount.track_rate_index = order.index(sel) if sel in order else 0
        self.push(vec, state=PropState.OK)

    async def _w_motion_ns(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        sel = vec.selected  # type: ignore[attr-defined]
        self.rig.mount.jog_dec = 1 if sel == "MOTION_NORTH" else (-1 if sel == "MOTION_SOUTH" else 0)
        self.push(vec, state=PropState.BUSY if sel else PropState.IDLE)

    async def _w_motion_we(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        sel = vec.selected  # type: ignore[attr-defined]
        # West decreases RA.
        self.rig.mount.jog_ra = -1 if sel == "MOTION_WEST" else (1 if sel == "MOTION_EAST" else 0)
        self.push(vec, state=PropState.BUSY if sel else PropState.IDLE)

    async def _w_slew_rate(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        order = ["SLEW_GUIDE", "SLEW_CENTERING", "SLEW_FIND", "SLEW_MAX"]
        sel = vec.selected  # type: ignore[attr-defined]
        self.rig.mount.slew_rate_index = order.index(sel) if sel in order else 2
        self.push(vec, state=PropState.OK)

    async def _w_guide_rate(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        # Per axis: a client that sets a slower Dec rate must get a slower Dec
        # axis, otherwise its calibration and its corrections disagree.
        self.rig.mount.guide_rate_we = float(vec["GUIDE_RATE_WE"].value)
        self.rig.mount.guide_rate_ns = float(vec["GUIDE_RATE_NS"].value)
        self.push(vec, state=PropState.OK)

    async def _w_offset_rates(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        self.rig.mount.ra_rate = float(vec["OFFSET_RATE_RA"].value)
        self.rig.mount.dec_rate = float(vec["OFFSET_RATE_DEC"].value)
        self.push(vec, state=PropState.OK)

    async def _w_park(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        m = self.rig.mount
        if vec.selected == "PARK":  # type: ignore[attr-defined]
            m.parked = False  # allow the slew, then latch
            self.rig.slew_to(m.park_ra_deg, m.park_dec_deg)
            m.tracking = False
            self._parking = True
            # slew_to turned tracking back on; the client has to be told it is
            # off again, otherwise it shows a parked mount as tracking.
            self._push_track_state()
            self.push(vec, state=PropState.BUSY, message="parking")
        else:
            m.parked = False
            self._parking = False
            self.push(vec, state=PropState.OK, message="unparked")
            self.push(self.eq, state=self._eq_state())

    _parking = False

    async def _w_park_position(self, vec: Vector, values: dict[str, str]) -> None:
        m = self.rig.mount
        if "PARK_RA" in values:
            vec["PARK_RA"].value = parse_number(values["PARK_RA"])
            m.park_ra_deg = float(vec["PARK_RA"].value) * 15.0
        if "PARK_DEC" in values:
            vec["PARK_DEC"].value = parse_number(values["PARK_DEC"])
            m.park_dec_deg = float(vec["PARK_DEC"].value)
        self.push(vec, state=PropState.OK)

    async def _w_park_option(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        sel = vec.selected  # type: ignore[attr-defined]
        m = self.rig.mount
        if sel == "PARK_CURRENT":
            m.park_ra_deg, m.park_dec_deg = m.ra_deg, m.dec_deg
        elif sel == "PARK_DEFAULT":
            m.park_ra_deg = self.rig.cfg.mount.park_ra_hours * 15.0
            m.park_dec_deg = self.rig.cfg.mount.park_dec_deg
        self.park_position["PARK_RA"].value = m.park_ra_deg / 15.0
        self.park_position["PARK_DEC"].value = m.park_dec_deg
        self.push(self.park_position, state=PropState.OK)
        for it in vec.items:
            it.value = False
        self.push(vec, state=PropState.OK)

    async def _w_home(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        sel = vec.selected  # type: ignore[attr-defined]
        if sel in ("FIND_HOME", "GO_HOME"):
            self.rig.slew_to(self.rig.lst_deg, self.rig.cfg.site.latitude)
            self._homing = True
            self.push(vec, state=PropState.BUSY, message="homing")
            return
        for it in vec.items:
            it.value = False
        self.push(vec, state=PropState.OK)

    _homing = False

    async def _w_geo(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        site = self.rig.cfg.site
        site.latitude = float(vec["LAT"].value)
        lon = float(vec["LONG"].value)
        site.longitude = lon - 360.0 if lon > 180 else lon
        site.elevation = float(vec["ELEV"].value)
        self.push(vec, state=PropState.OK)

    async def _w_time(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = v
        self.push(vec, state=PropState.OK)

    async def _w_info(self, vec: Vector, values: dict[str, str]) -> None:
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        tel = self.rig.cfg.telescope
        tel.aperture_mm = float(vec["TELESCOPE_APERTURE"].value)
        tel.focal_length_mm = float(vec["TELESCOPE_FOCAL_LENGTH"].value)
        # Ekos sends all four fields from its optical train, so the guide scope
        # follows too - otherwise the guider keeps a plate scale the client has
        # just told us is wrong.
        tel.guide_aperture_mm = float(vec["GUIDER_APERTURE"].value)
        tel.guide_focal_length_mm = float(vec["GUIDER_FOCAL_LENGTH"].value)
        self.push(vec, state=PropState.OK)

    # -- periodic ----------------------------------------------------------
    def _eq_state(self) -> PropState:
        """The state a client reads the mount's *status* out of.

        Ekos does not use ``TELESCOPE_TRACK_STATE`` for its status display: it
        maps the state attribute of ``EQUATORIAL_EOD_COORD`` alone (Idle ->
        parked or idle, Ok -> tracking, Busy -> slewing, or parking when
        ``TELESCOPE_PARK`` is Busy too). Reporting Ok whenever the mount is not
        slewing therefore shows a parked, stationary mount as tracking, and
        overwrites the switch a moment after the user pressed Off. This mirrors
        INDI::Telescope::NewRaDec, which is the behaviour every real driver has.
        """
        m = self.rig.mount
        if m.slewing:
            return PropState.BUSY
        if m.parked or not m.tracking:
            return PropState.IDLE
        return PropState.OK

    def _push_track_state(self) -> None:
        """Report ``TELESCOPE_TRACK_STATE`` from the rig, not from the switch.

        Tracking is turned off by parking and back on by any slew, so the
        switch a client last wrote is not the truth for long.
        """
        tracking = self.rig.mount.tracking
        if (
            self.track_state["TRACK_ON"].value == tracking
            and self.track_state["TRACK_OFF"].value != tracking
        ):
            return
        self.track_state["TRACK_ON"].value = tracking
        self.track_state["TRACK_OFF"].value = not tracking
        self.push(self.track_state, state=PropState.OK)

    def _push_pier_side(self) -> None:
        side = self.rig.mount.pier_side
        self.pier_side["PIER_EAST"].value = side == 1
        self.pier_side["PIER_WEST"].value = side == 2
        self.push(self.pier_side)

    async def step(self, dt: float) -> None:
        m = self.rig.mount
        was_slewing = self.eq.state is PropState.BUSY

        # Settle before the position goes out: parking latches ``parked`` and a
        # SLEW clears ``tracking``, and both change the state the client reads
        # its status from. Pushing the position first sends one stale Ok, which
        # is the whole frame Ekos needs to latch "tracking".
        if was_slewing and not m.slewing:
            if self._stop_tracking_after_slew:
                m.tracking = False
                self._stop_tracking_after_slew = False
            if self._parking:
                m.parked = True
                self._parking = False
                self.push(self.park, state=PropState.OK, message="parked")
            if self._homing:
                m.at_home = True
                self._homing = False
                for it in self.home.items:
                    it.value = False
                self.push(self.home, state=PropState.OK, message="at home")
            self._push_pier_side()

        self.eq["RA"].value = m.ra_deg / 15.0
        self.eq["DEC"].value = m.dec_deg
        state = self._eq_state()
        # A status change has to go out on the tick it happened, not on the
        # next two-second heartbeat.
        changed = state is not self.eq.state
        # While slewing, clients expect a steady stream of positions.
        if m.slewing or was_slewing or changed or int(self.rig.elapsed_s) % 2 == 0:
            self.push(self.eq, state=state)
        self._push_track_state()

        az, alt = self.rig.altaz()
        self.horiz["ALT"].value = alt
        self.horiz["AZ"].value = az
        self.time_lst["LST"].value = self.rig.lst_deg / 15.0
        if int(self.rig.elapsed_s * 2) % 4 == 0:
            self.push(self.horiz)
            self.push(self.time_lst)
            self.time_utc["UTC"].value = self.rig.iso_utc
            self.push(self.time_utc)

        self.step_guide_pulse()
