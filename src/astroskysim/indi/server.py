"""asyncio INDI server.

We are a *server*, not a driver: we listen on 7624 ourselves rather than being
launched by ``indiserver`` on stdin/stdout. Clients cannot tell the difference.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .protocol import parse_number
from .xml_stream import Element, XmlStreamError, XmlStreamSplitter, parse_element

if TYPE_CHECKING:
    from ..rig import Rig
    from .device import Device

log = logging.getLogger("astroskysim.indi")

DEFAULT_PORT = 7624

#: Maximum queued BLOBs per client. Frames are large and each one is a distinct
#: event, so these cannot be coalesced - a slow client loses the oldest.
BLOB_QUEUE_LIMIT = 4

#: Maximum queued discrete non-BLOB events (messages, definitions) per client.
EVENT_QUEUE_LIMIT = 256

#: Largest simulated step a single tick may take, in seconds. The tick steps by
#: wall-clock time so a blocked loop does not slow the simulated clock; this
#: bounds what one recovery step can do, so a stall cannot teleport a slew.
MAX_STEP_S = 0.5


class BlobPolicy:
    NEVER = "Never"
    ALSO = "Also"
    ONLY = "Only"


class OutQueue:
    """Coalescing output queue.

    An INDI ``set*Vector`` carries the *current* value of a property, so a newer
    one supersedes any still-queued older one for the same property. Coalescing
    on ``(device, property)`` therefore both bounds the queue and - crucially -
    means a terminal state transition can never be evicted by the stream of
    position updates that preceded it. A plain bounded queue drops the
    ``state="Ok"`` that tells a client the slew finished, and the client hangs.

    Definitions, messages and BLOBs are discrete events and are never merged.
    """

    def __init__(self) -> None:
        # dict preserves insertion order, and re-assigning an existing key keeps
        # that key's original position while refreshing its payload.
        self._items: dict[object, str] = {}
        self._seq = 0
        self._blobs = 0
        self._wake = asyncio.Event()
        self.dropped_blobs = 0
        self.dropped_events = 0

    def __len__(self) -> int:
        return len(self._items)

    def put(self, xml: str, *, key: object | None = None, is_blob: bool = False) -> None:
        if is_blob:
            if self._blobs >= BLOB_QUEUE_LIMIT:
                oldest = next((k for k in self._items if isinstance(k, _BlobKey)), None)
                if oldest is not None:
                    del self._items[oldest]
                    self._blobs -= 1
                    self.dropped_blobs += 1
            self._seq += 1
            self._items[_BlobKey(self._seq)] = xml
            self._blobs += 1
        elif key is not None:
            self._items[key] = xml  # coalesce
        else:
            if len(self._items) >= EVENT_QUEUE_LIMIT:
                self.dropped_events += 1
                return
            self._seq += 1
            self._items[self._seq] = xml
        self._wake.set()

    async def get(self) -> str:
        while not self._items:
            self._wake.clear()
            await self._wake.wait()
        key = next(iter(self._items))
        xml = self._items.pop(key)
        if isinstance(key, _BlobKey):
            self._blobs -= 1
        return xml


class _BlobKey(int):
    """Marks a queue entry as a BLOB, so it can be counted and evicted."""

    __slots__ = ()


class ClientSession:
    """Per-connection state: subscriptions, BLOB policy, output queue, buffer."""

    _next_id = 0

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ClientSession._next_id += 1
        self.id = ClientSession._next_id
        self.reader = reader
        self.writer = writer
        self.splitter = XmlStreamSplitter()
        self.blob_policy = BlobPolicy.NEVER
        self.out = OutQueue()
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"

        #: What this client asked for, as ``(device, property)`` pairs; an empty
        #: property means the whole device. ``getProperties`` with no device at
        #: all sets ``all_props``. A client must see only what it subscribed to:
        #: sending it the whole rig is harmless to a plain client but breaks
        #: ``indiserver`` chaining, where each connection is scoped to one
        #: device by ``dev@host:port`` and everything else is stray traffic.
        self.all_props = False
        self.props: set[tuple[str, str]] = set()

        #: ``(device, property)`` pairs this client has been sent a
        #: ``def*Vector`` for. A ``set*Vector`` for anything else is unusable -
        #: the client has no definition to apply it to - so the definition goes
        #: out ahead of it rather than the value being dropped.
        self.seen_defs: set[tuple[str, str]] = set()

    @property
    def dropped(self) -> int:
        return self.out.dropped_blobs + self.out.dropped_events

    def subscribe(self, device: str, name: str) -> None:
        """Record a ``getProperties`` filter."""
        if not device:
            self.all_props = True
        else:
            self.props.add((device, name))

    def subscribed(self, device: str, name: str = "") -> bool:
        """Does this client's filter cover ``(device, name)``?

        An empty ``name`` asks the device-level question, which is what a
        ``<message>`` needs: any subscription to the device is enough.
        """
        if self.all_props:
            return True
        if not name:
            return any(d == device for d, _ in self.props)
        return (device, "") in self.props or (device, name) in self.props

    def wants(self, *, is_blob: bool) -> bool:
        if is_blob:
            return self.blob_policy in (BlobPolicy.ALSO, BlobPolicy.ONLY)
        return self.blob_policy != BlobPolicy.ONLY

    def enqueue(
        self, xml: str, *, is_blob: bool = False, key: object | None = None
    ) -> None:
        if not self.wants(is_blob=is_blob):
            return
        self.out.put(xml, key=key, is_blob=is_blob)


class IndiServer:
    def __init__(self, rig: Rig, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
        self.rig = rig
        self.host = host
        self.port = port
        self.devices: list[Device] = []
        self.sessions: set[ClientSession] = set()
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()

    # -- registration ------------------------------------------------------
    def add_device(self, device: Device) -> Device:
        device.server = self
        self.devices.append(device)
        return device

    def find(self, name: str) -> Device | None:
        return next((d for d in self.devices if d.device_name == name), None)

    @property
    def active_connections(self) -> int:
        return len(self.sessions)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._on_client, self.host, self.port)
        log.info(
            "INDI server on %s:%d, devices: %s",
            self.host,
            self.port,
            ", ".join(d.device_name for d in self.devices),
        )

    async def stop(self) -> None:
        """Shut down, in an order that cannot deadlock.

        ``wait_closed()`` waits for every connection handler to return, and a
        handler sits in ``reader.read()`` until its socket closes. So the client
        sockets must be closed *first*; closing them after is a guaranteed hang
        whenever a client is still attached.
        """
        if self._server is not None:
            self._server.close()  # stop accepting, but existing handlers live on

        for t in list(self._tasks):
            t.cancel()

        for s in list(self.sessions):
            with contextlib.suppress(Exception):
                s.writer.close()
        # Let the handlers observe EOF and unwind.
        await asyncio.sleep(0)

        if self._server is not None:
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except (TimeoutError, Exception):
                log.warning("server sockets did not close cleanly")
            self._server = None

        self.sessions.clear()
        self._tasks.clear()

    async def serve_forever(self, tick_hz: float = 10.0) -> None:
        await self.start()
        self._spawn(self._tick(1.0 / tick_hz))
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _tick(self, period: float) -> None:
        """Drive the simulation. Errors are logged, never fatal.

        The step is the **wall-clock** time since the previous tick, not
        ``period``. Anything that blocks the loop - a survey reprojection, a
        FITS encode, a slow client - stretches the interval, and stepping by the
        nominal period instead would silently delete that time from the
        simulated clock. The client measures drift and guide-pulse response in
        real seconds, so a simulated clock running at a fraction of real time
        (and at a *varying* fraction, depending on what is rendering) makes
        every rate it derives wrong and its guiding oscillate.

        The step is capped at ``MAX_STEP_S``: after a genuine stall a single
        unbounded step would teleport a slew rather than simulate it. Losing the
        excess is the lesser error, and it is logged.
        """
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(period)
            now = loop.time()
            dt, last = now - last, now
            if dt > MAX_STEP_S:
                log.warning(
                    "simulation stalled for %.2f s; stepping %.2f s and dropping "
                    "the rest", dt, MAX_STEP_S,
                )
                dt = MAX_STEP_S
            try:
                await self.rig.step(dt)
                for d in self.devices:
                    await d.step(dt)
            except Exception:
                log.exception("simulation tick failed")

    # -- fan out -----------------------------------------------------------
    def broadcast(
        self,
        xml: str,
        dev: str,
        prop: str = "",
        *,
        key: object | None = None,
        def_xml: Callable[[], str] | None = None,
    ) -> None:
        """Fan out a value to every client subscribed to this property.

        An empty ``prop`` means device-level - a ``<message>`` - and reaches any
        client subscribed to the device. ``key`` identifies a property so
        successive updates coalesce; pass None for discrete events that must all
        be delivered. ``def_xml`` builds the definition on demand, for the case
        where a property becomes visible only after a client's
        ``getProperties``: the client would have no definition to attach the
        value to, so the definition is sent first instead of losing the update.
        """
        is_blob = xml.startswith("<setBLOBVector")
        for s in self.sessions:
            if not s.subscribed(dev, prop):
                continue
            if prop and def_xml is not None and (dev, prop) not in s.seen_defs:
                s.enqueue(def_xml())
                s.seen_defs.add((dev, prop))
            s.enqueue(xml, is_blob=is_blob, key=key)

    def broadcast_def(self, xml: str, dev: str, prop: str) -> None:
        """Announce (or re-announce) a property definition to its subscribers."""
        for s in self.sessions:
            if s.subscribed(dev, prop):
                s.enqueue(xml)
                s.seen_defs.add((dev, prop))

    # -- connection handling -----------------------------------------------
    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session = ClientSession(reader, writer)
        self.sessions.add(session)
        log.info("client %s connected from %s", session.id, session.peer)
        pump = self._spawn(self._pump(session))
        try:
            await self._read_loop(session)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("client %s read loop failed", session.id)
        finally:
            pump.cancel()
            self.sessions.discard(session)
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            log.info(
                "client %s disconnected (%d dropped)", session.id, session.dropped
            )

    async def _pump(self, session: ClientSession) -> None:
        """Serialise this client's outbound queue onto the socket."""
        try:
            while True:
                xml = await session.out.get()
                session.writer.write(xml.encode("utf-8"))
                await session.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("client %s write pump failed", session.id)

    async def _read_loop(self, session: ClientSession) -> None:
        while True:
            chunk = await session.reader.read(65536)
            if not chunk:
                return
            try:
                raws = session.splitter.feed(chunk)
            except XmlStreamError as exc:
                log.warning("client %s: %s - resynchronising", session.id, exc)
                continue
            for raw in raws:
                try:
                    el = parse_element(raw)
                except XmlStreamError as exc:
                    log.warning("client %s sent malformed element: %s", session.id, exc)
                    continue
                await self._dispatch(session, el)

    async def _dispatch(self, session: ClientSession, el: Element) -> None:
        tag = el.tag

        if tag == "getProperties":
            await self._get_properties(session, el)
            return

        if tag == "enableBLOB":
            value = (el.text or "").strip() or BlobPolicy.NEVER
            if value in (BlobPolicy.NEVER, BlobPolicy.ALSO, BlobPolicy.ONLY):
                session.blob_policy = value
                log.debug("client %s BLOB policy -> %s", session.id, value)
            return

        if tag.startswith("new") and tag.endswith("Vector"):
            device = self.find(el.device)
            if device is None:
                return
            values = el.child_values()
            if tag == "newNumberVector":
                # Validate early so a device handler can assume clean floats.
                bad = [k for k, v in values.items() if not _is_number(v)]
                if bad:
                    log.warning("client %s sent non-numeric %s", session.id, bad)
                    return
            await device.handle_write(el.name, values)
            return

        log.debug("client %s sent unhandled element <%s>", session.id, tag)

    async def _get_properties(self, session: ClientSession, el: Element) -> None:
        want_device = el.device
        want_name = el.name
        session.subscribe(want_device, want_name)

        if want_device and self.find(want_device) is None:
            # Worth a warning: a client asking for a device we do not have gets
            # silence, and a typo in the name looks exactly like a dead server.
            log.warning(
                "client %s asked for unknown device %r; we have: %s",
                session.id,
                want_device,
                ", ".join(d.device_name for d in self.devices),
            )
            return

        for d in self.devices:
            if want_device and d.device_name != want_device:
                continue
            for name, xml in d.defs(want_name):
                session.seen_defs.add((d.device_name, name))
                session.enqueue(xml)


def _is_number(text: str) -> bool:
    try:
        parse_number(text)
    except ValueError:
        return False
    return True
