"""Focuser.

Absolute and relative moves, sync, reverse, temperature compensation and
backlash. The rig models backlash physically (``focus_backlash``), so a client
that compensates for it is tested against a focuser that really has it rather
than against a property it can only read back.
"""

from __future__ import annotations

import numpy as np

from ..indi.device import FOCUSER_INTERFACE, Device
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


class Focuser(Device):
    device_name = "AstroSkySim Focuser"
    interface = FOCUSER_INTERFACE

    def setup(self) -> None:
        cfg = self.rig.cfg.focuser
        f = self.rig.focuser

        self.absolute = self.add(
            NumberVector(
                name="ABS_FOCUS_POSITION",
                label="Absolute Position",
                items=[
                    NumberItem("FOCUS_ABSOLUTE_POSITION", "Steps", f.position, "%.0f", 0, cfg.max_position, 1)
                ],
            )
        )
        self.relative = self.add(
            NumberVector(
                name="REL_FOCUS_POSITION",
                label="Relative Position",
                items=[
                    NumberItem("FOCUS_RELATIVE_POSITION", "Steps", 0, "%.0f", 0, cfg.max_position, 1)
                ],
            )
        )
        self.motion = self.add(
            SwitchVector(
                name="FOCUS_MOTION",
                label="Direction",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("FOCUS_INWARD", "Inward", True),
                    SwitchItem("FOCUS_OUTWARD", "Outward"),
                ],
            )
        )
        self.abort = self.add(
            SwitchVector(
                name="FOCUS_ABORT_MOTION",
                label="Abort",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("ABORT", "Abort")],
            )
        )
        self.max_pos = self.add(
            NumberVector(
                name="FOCUS_MAX",
                label="Max Position",
                group="Options",
                items=[NumberItem("FOCUS_MAX_VALUE", "Steps", cfg.max_position, "%.0f", 1, 1e6, 1)],
            )
        )
        self.sync = self.add(
            NumberVector(
                name="FOCUS_SYNC",
                label="Sync",
                group="Options",
                items=[NumberItem("FOCUS_SYNC_VALUE", "Steps", f.position, "%.0f", 0, cfg.max_position, 1)],
            )
        )
        self.reverse = self.add(
            SwitchVector(
                name="FOCUS_REVERSE_MOTION",
                label="Reverse",
                group="Options",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("INDI_ENABLED", "Enabled"),
                    SwitchItem("INDI_DISABLED", "Disabled", True),
                ],
            )
        )
        self.temperature = self.add(
            NumberVector(
                name="FOCUS_TEMPERATURE",
                label="Temperature",
                group="Main Control",
                perm=Perm.RO,
                items=[NumberItem("TEMPERATURE", "Temp (C)", cfg.temperature, "%.1f", -50, 60, 0.1)],
            )
        )
        # Temperature compensation, paired with FOCUS_TEMPERATURE above.
        self.temp_comp = self.add(
            SwitchVector(
                name="FOCUS_TEMPERATURE_COMPENSATION",
                label="Temp. Compensation",
                group="Options",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("INDI_ENABLED", "Enabled"),
                    SwitchItem("INDI_DISABLED", "Disabled", True),
                ],
            )
        )
        # Backlash is simulated physically by the rig, so this toggle bites.
        self.backlash_toggle = self.add(
            SwitchVector(
                name="FOCUS_BACKLASH_TOGGLE",
                label="Backlash",
                group="Options",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("INDI_ENABLED", "Enabled", cfg.backlash > 0),
                    SwitchItem("INDI_DISABLED", "Disabled", cfg.backlash == 0),
                ],
            )
        )
        self.backlash_steps = self.add(
            NumberVector(
                name="FOCUS_BACKLASH_STEPS",
                label="Backlash Steps",
                group="Options",
                items=[NumberItem("FOCUS_BACKLASH_VALUE", "Steps", cfg.backlash, "%.0f", 0, 1000, 1)],
            )
        )

        for name, fn in (
            ("ABS_FOCUS_POSITION", self._w_abs),
            ("REL_FOCUS_POSITION", self._w_rel),
            ("FOCUS_MOTION", self._w_ok),
            ("FOCUS_ABORT_MOTION", self._w_abort),
            ("FOCUS_MAX", self._w_max),
            ("FOCUS_SYNC", self._w_sync),
            ("FOCUS_REVERSE_MOTION", self._w_reverse),
            ("FOCUS_TEMPERATURE_COMPENSATION", self._w_temp_comp),
            ("FOCUS_BACKLASH_TOGGLE", self._w_backlash_toggle),
            ("FOCUS_BACKLASH_STEPS", self._w_backlash_steps),
        ):
            self.writer(name, fn)

    async def _w_ok(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)

    async def _w_abs(self, vec: Vector, values: dict[str, str]) -> None:
        target = parse_number(values.get("FOCUS_ABSOLUTE_POSITION", "0"))
        self.rig.move_focuser(target)
        vec["FOCUS_ABSOLUTE_POSITION"].value = self.rig.focuser.position
        self.push(vec, state=PropState.BUSY)

    async def _w_rel(self, vec: Vector, values: dict[str, str]) -> None:
        steps = parse_number(values.get("FOCUS_RELATIVE_POSITION", "0"))
        inward = self.motion.selected == "FOCUS_INWARD"  # type: ignore[attr-defined]
        if self.rig.focuser.reversed:
            inward = not inward
        delta = -steps if inward else steps
        vec["FOCUS_RELATIVE_POSITION"].value = steps
        self.rig.move_focuser(self.rig.focuser.position + delta)
        self.push(vec, state=PropState.BUSY)

    async def _w_abort(self, vec: Vector, values: dict[str, str]) -> None:
        f = self.rig.focuser
        f.moving = False
        f.target = f.position
        for it in vec.items:
            it.value = False
        self.push(vec, state=PropState.OK, message="focuser stopped")
        self.push(self.absolute, state=PropState.IDLE)

    async def _w_max(self, vec: Vector, values: dict[str, str]) -> None:
        v = parse_number(values.get("FOCUS_MAX_VALUE", "0"))
        self.rig.cfg.focuser.max_position = int(max(v, 1))
        vec["FOCUS_MAX_VALUE"].value = self.rig.cfg.focuser.max_position
        # Limits changed, so the definition must be re-announced.
        self.absolute["FOCUS_ABSOLUTE_POSITION"].max = float(self.rig.cfg.focuser.max_position)
        self.push(vec, state=PropState.OK)
        self.push_def(self.absolute)

    async def _w_sync(self, vec: Vector, values: dict[str, str]) -> None:
        v = parse_number(values.get("FOCUS_SYNC_VALUE", "0"))
        f = self.rig.focuser
        f.position = float(np.clip(v, 0, self.rig.cfg.focuser.max_position))
        f.target = f.position
        f.moving = False
        vec["FOCUS_SYNC_VALUE"].value = f.position
        self.absolute["FOCUS_ABSOLUTE_POSITION"].value = f.position
        self.push(vec, state=PropState.OK)
        self.push(self.absolute, state=PropState.OK)

    async def _w_reverse(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.rig.focuser.reversed = vec.selected == "INDI_ENABLED"  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)

    async def _w_temp_comp(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        enabled = vec.selected == "INDI_ENABLED"  # type: ignore[attr-defined]
        self.rig.focuser.temp_comp = enabled
        self._comp_reference = (
            float(self.temperature["TEMPERATURE"].value) if enabled else None
        )
        self.push(vec, state=PropState.OK)

    _comp_reference: float | None = None

    async def _w_backlash_toggle(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        enabled = vec.selected == "INDI_ENABLED"  # type: ignore[attr-defined]
        self.rig.focuser.backlash = (
            int(self.backlash_steps["FOCUS_BACKLASH_VALUE"].value) if enabled else 0
        )
        self.push(vec, state=PropState.OK)

    async def _w_backlash_steps(self, vec: Vector, values: dict[str, str]) -> None:
        v = parse_number(values.get("FOCUS_BACKLASH_VALUE", "0"))
        vec["FOCUS_BACKLASH_VALUE"].value = max(v, 0)
        if self.backlash_toggle.selected == "INDI_ENABLED":  # type: ignore[attr-defined]
            self.rig.focuser.backlash = int(vec["FOCUS_BACKLASH_VALUE"].value)
        self.push(vec, state=PropState.OK)

    async def step(self, dt: float) -> None:
        f = self.rig.focuser
        was_moving = self.absolute.state is PropState.BUSY

        if f.temp_comp and self._comp_reference is not None:
            temp = float(self.temperature["TEMPERATURE"].value)
            drift = (temp - self._comp_reference) * self.rig.cfg.focuser.temp_coeff
            if abs(drift) >= 1.0 and not f.moving:
                self._comp_reference = temp
                self.rig.move_focuser(f.position + drift)

        self.absolute["FOCUS_ABSOLUTE_POSITION"].value = f.position
        if f.moving:
            self.push(self.absolute, state=PropState.BUSY)
        elif was_moving:
            self.push(self.absolute, state=PropState.OK)
            if self.relative.state is PropState.BUSY:
                self.push(self.relative, state=PropState.OK)
