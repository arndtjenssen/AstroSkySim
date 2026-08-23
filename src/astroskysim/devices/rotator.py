"""Rotator.

Absolute and relative moves, plus the mechanical-versus-sky angle
distinction: the sky position angle is what a client frames with, the mechanical
angle is where the motor actually sits. The two differ by
``rotator.mechanical_offset``.
"""

from __future__ import annotations

from ..indi.device import ROTATOR_INTERFACE, Device
from ..indi.protocol import (
    NumberItem,
    NumberVector,
    Perm,
    PropState,
    SwitchItem,
    SwitchRule,
    SwitchVector,
    Vector,
    parse_number,
)


class Rotator(Device):
    device_name = "AstroSkySim Rotator"
    interface = ROTATOR_INTERFACE

    def setup(self) -> None:
        r = self.rig.rotator

        self.absolute = self.add(
            NumberVector(
                name="ABS_ROTATOR_ANGLE",
                label="Angle",
                items=[NumberItem("ANGLE", "Sky PA (deg)", r.angle, "%.3f", 0, 360, 0.01)],
            )
        )
        # Relative move, in degrees from the current angle.
        self.relative = self.add(
            NumberVector(
                name="REL_ROTATOR_ANGLE",
                label="Relative Angle",
                items=[NumberItem("ANGLE", "Delta (deg)", 0, "%.3f", -180, 180, 0.01)],
            )
        )
        # Mechanical angle, offset from the sky PA by rotator.mechanical_offset.
        self.mechanical = self.add(
            NumberVector(
                name="ROTATOR_MECHANICAL_ANGLE",
                label="Mechanical Angle",
                perm=Perm.RO,
                items=[NumberItem("ANGLE", "Mechanical (deg)", r.angle, "%.3f", 0, 360, 0.01)],
            )
        )
        self.sync = self.add(
            NumberVector(
                name="SYNC_ROTATOR_ANGLE",
                label="Sync",
                group="Options",
                items=[NumberItem("ANGLE", "Sky PA (deg)", r.angle, "%.3f", 0, 360, 0.01)],
            )
        )
        self.abort = self.add(
            SwitchVector(
                name="ROTATOR_ABORT_MOTION",
                label="Abort",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("ABORT", "Abort")],
            )
        )
        self.reverse = self.add(
            SwitchVector(
                name="ROTATOR_REVERSE",
                label="Reverse",
                group="Options",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("INDI_ENABLED", "Enabled", r.reversed),
                    SwitchItem("INDI_DISABLED", "Disabled", not r.reversed),
                ],
            )
        )

        for name, fn in (
            ("ABS_ROTATOR_ANGLE", self._w_abs),
            ("REL_ROTATOR_ANGLE", self._w_rel),
            ("SYNC_ROTATOR_ANGLE", self._w_sync),
            ("ROTATOR_ABORT_MOTION", self._w_abort),
            ("ROTATOR_REVERSE", self._w_reverse),
        ):
            self.writer(name, fn)

    def _mechanical_for(self, sky: float) -> float:
        """Invert the sky-angle mapping in ``Rig.sky_position_angle``."""
        a = sky - self.rig.cfg.rotator.mechanical_offset
        if self.rig.rotator.reversed:
            a = -a
        return a % 360.0

    async def _w_abs(self, vec: Vector, values: dict[str, str]) -> None:
        sky = parse_number(values.get("ANGLE", "0"))
        vec["ANGLE"].value = sky % 360.0
        self.rig.move_rotator(self._mechanical_for(sky))
        self.push(vec, state=PropState.BUSY)

    async def _w_rel(self, vec: Vector, values: dict[str, str]) -> None:
        delta = parse_number(values.get("ANGLE", "0"))
        vec["ANGLE"].value = delta
        self.rig.move_rotator(self.rig.rotator.angle + delta)
        self.push(vec, state=PropState.BUSY)
        self.push(self.absolute, state=PropState.BUSY)

    async def _w_sync(self, vec: Vector, values: dict[str, str]) -> None:
        sky = parse_number(values.get("ANGLE", "0"))
        r = self.rig.rotator
        r.angle = self._mechanical_for(sky)
        r.target = r.angle
        r.moving = False
        vec["ANGLE"].value = sky % 360.0
        self.absolute["ANGLE"].value = self.rig.sky_position_angle
        self.mechanical["ANGLE"].value = r.angle
        self.push(vec, state=PropState.OK)
        self.push(self.absolute, state=PropState.OK)
        self.push(self.mechanical)

    async def _w_abort(self, vec: Vector, values: dict[str, str]) -> None:
        r = self.rig.rotator
        r.moving = False
        r.target = r.angle
        for it in vec.items:
            it.value = False
        self.push(vec, state=PropState.OK, message="rotator stopped")
        self.push(self.absolute, state=PropState.IDLE)

    async def _w_reverse(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.rig.rotator.reversed = vec.selected == "INDI_ENABLED"  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)
        self.absolute["ANGLE"].value = self.rig.sky_position_angle
        self.push(self.absolute)

    async def step(self, dt: float) -> None:
        r = self.rig.rotator
        was_moving = self.absolute.state is PropState.BUSY
        self.absolute["ANGLE"].value = self.rig.sky_position_angle
        self.mechanical["ANGLE"].value = r.angle
        if r.moving:
            self.push(self.absolute, state=PropState.BUSY)
            self.push(self.mechanical)
        elif was_moving:
            self.push(self.absolute, state=PropState.OK)
            self.push(self.mechanical)
            if self.relative.state is PropState.BUSY:
                self.push(self.relative, state=PropState.OK)
