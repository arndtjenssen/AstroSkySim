"""Device base class.

One device object is shared by **all** clients; only the per-connection state
(which properties have been announced, the BLOB policy, the output queue) lives
in the client session. The alternative — a fresh instance of every device class
per connection — forces the simulated state into globals so the instances agree
with each other, and any gap in that bookkeeping shows up as two clients
disagreeing about where the telescope is pointing. Sharing the object makes the
agreement structural instead, which is the whole point of a rig simulator.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from .protocol import (
    Perm,
    PropState,
    TextItem,
    TextVector,
    Vector,
    config_process_vector,
    connection_vector,
    message_xml,
)

if TYPE_CHECKING:
    from ..rig import Rig
    from .server import IndiServer

# DRIVER_INTERFACE bitmask, matching indiapi.h.
TELESCOPE_INTERFACE = 1 << 0
CCD_INTERFACE = 1 << 1
GUIDER_INTERFACE = 1 << 2
FOCUSER_INTERFACE = 1 << 3
FILTER_INTERFACE = 1 << 4
ROTATOR_INTERFACE = 1 << 12

WriteHandler = Callable[[Vector, dict[str, str]], Coroutine[Any, Any, None]]


class Device:
    """One simulated INDI device, shared across client connections."""

    #: Name as it appears to clients. Overridden per device.
    device_name = "Device"
    interface = 0
    driver_version = "0.1.0"

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.server: IndiServer | None = None
        self.vectors: dict[str, Vector] = {}
        self._writers: dict[str, WriteHandler] = {}

        self.connection = self.add(connection_vector())
        self.driver_info = self.add(
            TextVector(
                name="DRIVER_INFO",
                label="Driver Info",
                group="General Info",
                perm=Perm.RO,
                state=PropState.IDLE,
                items=[
                    TextItem("DRIVER_NAME", "Name", self.device_name),
                    TextItem("DRIVER_EXEC", "Exec", "astroskysim"),
                    TextItem("DRIVER_VERSION", "Version", self.driver_version),
                    TextItem("DRIVER_INTERFACE", "Interface", str(self.interface)),
                ],
            )
        )
        # Present on every device; accepted and acknowledged but not persisted.
        # Clients grey out their config buttons without it.
        self.config_process = self.add(config_process_vector())
        self._writers["CONNECTION"] = self._write_connection
        self._writers["CONFIG_PROCESS"] = self._write_config

        self.setup()

    # -- construction ------------------------------------------------------
    def setup(self) -> None:
        """Subclasses declare their properties here."""

    def add(self, vec: Vector) -> Vector:
        self.vectors[vec.name] = vec
        return vec

    def writer(self, vec_name: str, fn: WriteHandler) -> None:
        self._writers[vec_name] = fn

    # -- state -------------------------------------------------------------
    @property
    def connected(self) -> bool:
        item = self.connection.get("CONNECT")
        return bool(item and item.value)

    def defs(self, only: str = "") -> list[tuple[str, str]]:
        """``(property, def*Vector)`` per announced property, for a
        client's ``getProperties``.

        Kept as pairs rather than one concatenated string so the server can
        record exactly which definitions a client has been sent.
        """
        return [
            (v.name, v.def_xml(self.device_name))
            for v in self.vectors.values()
            if v.enabled and (not only or v.name == only)
        ]

    # -- outbound ----------------------------------------------------------
    def push(
        self,
        vec: Vector | str,
        *,
        state: PropState | None = None,
        message: str = "",
        only: list[str] | None = None,
    ) -> None:
        """Broadcast a ``set*Vector`` for ``vec`` to every connected client."""
        v = self.vectors[vec] if isinstance(vec, str) else vec
        if state is not None:
            v.state = state
        if self.server is not None and v.enabled:
            # Coalesce on (device, property): a newer value supersedes a queued
            # older one, so a slow client cannot miss a state transition.
            # A message is an event, so it opts out of coalescing.
            key = None if message else (self.device_name, v.name)
            self.server.broadcast(
                v.set_xml(self.device_name, message, only),
                self.device_name,
                v.name,
                key=key,
                # Built only for a client that has not been sent the definition
                # yet, so the common path costs nothing but the closure.
                def_xml=lambda: v.def_xml(self.device_name),
            )

    def push_def(self, vec: Vector | str) -> None:
        """Re-announce a property whose *definition* changed (limits, items)."""
        v = self.vectors[vec] if isinstance(vec, str) else vec
        if self.server is not None and v.enabled:
            self.server.broadcast_def(v.def_xml(self.device_name), self.device_name, v.name)

    def message(self, text: str) -> None:
        if self.server is not None:
            self.server.broadcast(message_xml(self.device_name, text), self.device_name)

    # -- inbound -----------------------------------------------------------
    async def handle_write(self, vec_name: str, values: dict[str, str]) -> None:
        vec = self.vectors.get(vec_name)
        if vec is None or not vec.enabled:
            return
        if vec.perm is Perm.RO:
            self.message(f"{vec_name} is read only")
            return
        handler = self._writers.get(vec_name)
        if handler is None:
            # Unhandled but writable: accept the value so the client's control
            # does not hang in Busy forever.
            vec.state = PropState.OK
            self.push(vec)
            return
        try:
            await handler(vec, values)
        except Exception as exc:  # a bad client write must not kill the server
            vec.state = PropState.ALERT
            self.push(vec, message=f"{vec_name}: {exc}")

    async def _write_connection(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        vec.state = PropState.OK
        self.push(vec)
        await (self.on_connect() if self.connected else self.on_disconnect())

    async def _write_config(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        for it in vec.items:
            it.value = False  # type: ignore[union-attr]
        vec.state = PropState.OK
        self.push(vec)

    async def on_connect(self) -> None:
        """Called after the client sets CONNECT=On."""

    async def on_disconnect(self) -> None:
        """Called after the client sets DISCONNECT=On."""

    # -- simulation --------------------------------------------------------
    async def step(self, dt: float) -> None:
        """Advance this device by ``dt`` seconds. Called from the rig tick."""
