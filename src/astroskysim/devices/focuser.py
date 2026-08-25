"""Focuser.

Absolute and relative moves, sync, reverse, temperature compensation and
backlash. The rig models backlash physically (``focus_backlash``), so a client
that compensates for it is tested against a focuser that really has it rather
than against a property it can only read back.

Temperature compensation is the same idea and only became real with
``[temperature]``: the probe is published from the model on its own cadence, the
focus point genuinely moves as the night cools, and ``focuser.temp_coeff`` is
what the *client* applies against it. Getting that coefficient wrong over- or
under-corrects, and getting it exactly right still leaves a residue, because the
probe follows the air and focus follows the optics.
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

#: How often the temperature probe is read, seconds. A real controller polls its
#: sensor on its own cadence rather than at the server tick, and the coalescing
#: output queue means a client would only ever see the newest value anyway - so
#: pushing at 10 Hz would be noise on the wire for nothing. It also means the
#: compensation loop below sees the temperature at the rate a real one does.
_PROBE_PERIOD_S = 4.0


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
        # A probe on the focuser body, which is where a ZWO EAF's sensor sits.
        # It reads the *air*, lagged a little - not the tube and glass, whose
        # temperature is what actually moves focus and which nothing reports.
        # That gap is the point; see ``TemperatureConfig``.
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

        self._probe_elapsed = _PROBE_PERIOD_S  # publish on the first tick

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

    def _read_probe(self, dt: float) -> None:
        """Publish the probe, on its own cadence rather than at the tick rate.

        Before this existed the item was seeded once in ``setup`` and never
        written again, so ``temp - self._comp_reference`` below was identically
        zero and the compensation branch could not fire at all.
        """
        self._probe_elapsed += dt
        if self._probe_elapsed < _PROBE_PERIOD_S:
            return
        self._probe_elapsed = 0.0
        model = self.rig.temperature
        value = self.rig.cfg.focuser.temperature if model is None else model.probe_c
        self.temperature["TEMPERATURE"].value = value
        self.push(self.temperature, state=PropState.OK)

    async def step(self, dt: float) -> None:
        f = self.rig.focuser
        was_moving = self.absolute.state is PropState.BUSY

        self._read_probe(dt)

        if f.temp_comp and self._comp_reference is not None:
            # ``reference - temp``, so a *positive* temp_coeff racks out as it
            # cools. That matches both the physical constant this compensates
            # (``temperature.focus_shift_um_per_c``, positive = cooling extends)
            # and what real controllers publish - an Optec coefficient is the
            # negative of the fitted position-versus-temperature slope, and its
            # default for an SCT is +86. So the perfectly calibrated value here
            # is ``focus_shift_um_per_c / step_size_um``.
            #
            # It still under-corrects, and that is the feature: this reads the
            # probe, which follows the air, while focus follows the optics.
            temp = float(self.temperature["TEMPERATURE"].value)
            drift = (self._comp_reference - temp) * self.rig.cfg.focuser.temp_coeff
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
