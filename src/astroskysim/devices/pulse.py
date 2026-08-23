"""Timed guide pulses, shared by every device that claims ``GUIDER_INTERFACE``.

In INDI, ``GUIDER_INTERFACE`` (bit 2) does not mean "this is a guide camera" —
it means "this device has an ST4-style port and accepts timed guide pulses".
Clients build their pulse-provider list from that bit, so the bit and the two
``TELESCOPE_TIMED_GUIDE_*`` properties have to travel together. Verified against
the reference drivers shipped with KStars:

* ``indi_simulator_telescope`` reports ``DRIVER_INTERFACE=5`` (TELESCOPE|GUIDER)
* ``indi_simulator_ccd``       reports ``DRIVER_INTERFACE=22`` (CCD|GUIDER|FILTER)

Both implement the pulse properties. Advertising the bit without them, or
implementing them without the bit, leaves the client with no working way to move
the mount during calibration — which fails as "star drift is too short".

The handlers write to ``rig.mount``, so a pulse has the same effect whichever
device the client happens to route it through.
"""

from __future__ import annotations

from ..indi.protocol import NumberItem, NumberVector, PropState, Vector, parse_number

#: Longest pulse a client may request, milliseconds.
MAX_PULSE_MS = 60000


class GuidePulseMixin:
    """Adds ``TELESCOPE_TIMED_GUIDE_NS`` / ``_WE`` and their handlers.

    Mix into a :class:`~astroskysim.indi.device.Device` and call
    :meth:`add_guide_pulse_properties` from ``setup()`` and
    :meth:`step_guide_pulse` from ``step()``.
    """

    def add_guide_pulse_properties(self, group: str = "Motion Control") -> None:
        self.guide_ns = self.add(
            NumberVector(
                name="TELESCOPE_TIMED_GUIDE_NS",
                label="Guide N/S",
                group=group,
                items=[
                    NumberItem("TIMED_GUIDE_N", "North (ms)", 0, "%.0f", 0, MAX_PULSE_MS, 10),
                    NumberItem("TIMED_GUIDE_S", "South (ms)", 0, "%.0f", 0, MAX_PULSE_MS, 10),
                ],
            )
        )
        self.guide_we = self.add(
            NumberVector(
                name="TELESCOPE_TIMED_GUIDE_WE",
                label="Guide W/E",
                group=group,
                items=[
                    NumberItem("TIMED_GUIDE_W", "West (ms)", 0, "%.0f", 0, MAX_PULSE_MS, 10),
                    NumberItem("TIMED_GUIDE_E", "East (ms)", 0, "%.0f", 0, MAX_PULSE_MS, 10),
                ],
            )
        )
        self.writer("TELESCOPE_TIMED_GUIDE_NS", self._w_guide_ns)
        self.writer("TELESCOPE_TIMED_GUIDE_WE", self._w_guide_we)

    async def _w_guide_ns(self, vec: Vector, values: dict[str, str]) -> None:
        n = parse_number(values.get("TIMED_GUIDE_N", "0"))
        s = parse_number(values.get("TIMED_GUIDE_S", "0"))
        m = self.rig.mount
        m.guide_ns_s = max(n, s) / 1000.0
        m.guide_ns_sign = 1 if n >= s else -1
        vec["TIMED_GUIDE_N"].value = n
        vec["TIMED_GUIDE_S"].value = s
        self.push(vec, state=PropState.BUSY if m.guide_ns_s > 0 else PropState.OK)

    async def _w_guide_we(self, vec: Vector, values: dict[str, str]) -> None:
        w = parse_number(values.get("TIMED_GUIDE_W", "0"))
        e = parse_number(values.get("TIMED_GUIDE_E", "0"))
        m = self.rig.mount
        m.guide_we_s = max(w, e) / 1000.0
        # West decreases RA, matching TELESCOPE_MOTION_WE.
        m.guide_we_sign = -1 if w >= e else 1
        vec["TIMED_GUIDE_W"].value = w
        vec["TIMED_GUIDE_E"].value = e
        self.push(vec, state=PropState.BUSY if m.guide_we_s > 0 else PropState.OK)

    def step_guide_pulse(self) -> None:
        """Release the BUSY the client is waiting on once a pulse has run out."""
        m = self.rig.mount
        if m.guide_ns_s <= 0 and self.guide_ns.state is PropState.BUSY:
            self.push(self.guide_ns, state=PropState.OK)
        if m.guide_we_s <= 0 and self.guide_we.state is PropState.BUSY:
            self.push(self.guide_we, state=PropState.OK)
