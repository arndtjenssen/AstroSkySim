"""Filter wheel.

Names, slot selection and per-filter focus offsets. The offsets are the point:
without them a client that refocuses per filter has nothing to read, and the
per-filter focus shift the rig applies to the imaging HFD looks like noise.
"""

from __future__ import annotations

from ..indi.device import FILTER_INTERFACE, Device
from ..indi.protocol import (
    NumberItem,
    NumberVector,
    PropState,
    TextItem,
    TextVector,
    Vector,
    parse_number,
)


class FilterWheel(Device):
    device_name = "AstroSkySim Filter Wheel"
    interface = FILTER_INTERFACE

    def setup(self) -> None:
        names = self.rig.cfg.filter_wheel.names
        offsets = self.rig.cfg.filter_wheel.focus_offsets

        self.slot = self.add(
            NumberVector(
                name="FILTER_SLOT",
                label="Filter",
                items=[
                    NumberItem("FILTER_SLOT_VALUE", "Slot", self.rig.filter.slot, "%.0f", 1, len(names), 1)
                ],
            )
        )
        self.names = self.add(
            TextVector(
                name="FILTER_NAME",
                label="Filter Names",
                group="Options",
                items=[
                    TextItem(f"FILTER_NAME_{i + 1}", f"Filter {i + 1}", n)
                    for i, n in enumerate(names)
                ],
            )
        )
        # Per-filter focus offsets; the rig folds these into the imaging HFD.
        self.offsets = self.add(
            NumberVector(
                name="FILTER_FOCUS_OFFSET",
                label="Focus Offsets",
                group="Options",
                items=[
                    NumberItem(
                        f"FILTER_OFFSET_{i + 1}", names[i], float(off), "%.0f", -10000, 10000, 1
                    )
                    for i, off in enumerate(offsets)
                ],
            )
        )

        self.writer("FILTER_SLOT", self._w_slot)
        self.writer("FILTER_NAME", self._w_names)
        self.writer("FILTER_FOCUS_OFFSET", self._w_offsets)

    async def _w_slot(self, vec: Vector, values: dict[str, str]) -> None:
        target = int(parse_number(values.get("FILTER_SLOT_VALUE", "1")))
        n = len(self.rig.cfg.filter_wheel.names)
        if not 1 <= target <= n:
            self.push(vec, state=PropState.ALERT, message=f"slot must be 1..{n}")
            return
        self.rig.select_filter(target)
        vec["FILTER_SLOT_VALUE"].value = self.rig.filter.slot
        self.push(vec, state=PropState.BUSY if self.rig.filter.moving else PropState.OK)

    async def _w_names(self, vec: Vector, values: dict[str, str]) -> None:
        cfg_names = self.rig.cfg.filter_wheel.names
        for k, v in values.items():
            if k in vec:
                vec[k].value = v
                idx = int(k.rsplit("_", 1)[-1]) - 1
                if 0 <= idx < len(cfg_names):
                    cfg_names[idx] = v
                    self.offsets.items[idx].label = v
        self.push(vec, state=PropState.OK)
        # Labels changed, so re-announce the offsets definition.
        self.push_def(self.offsets)

    async def _w_offsets(self, vec: Vector, values: dict[str, str]) -> None:
        cfg_offsets = self.rig.cfg.filter_wheel.focus_offsets
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
                idx = int(k.rsplit("_", 1)[-1]) - 1
                if 0 <= idx < len(cfg_offsets):
                    cfg_offsets[idx] = int(vec[k].value)
        self.push(vec, state=PropState.OK)

    async def step(self, dt: float) -> None:
        moving = self.rig.filter.moving
        was_moving = self.slot.state is PropState.BUSY
        self.slot["FILTER_SLOT_VALUE"].value = self.rig.filter.slot
        if moving:
            self.push(self.slot, state=PropState.BUSY)
        elif was_moving:
            self.push(self.slot, state=PropState.OK)
