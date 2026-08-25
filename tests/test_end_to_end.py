"""End-to-end: a real asyncio INDI client against a real server on a socket."""

from __future__ import annotations

import asyncio
import io
import time
import zlib

import numpy as np
import pytest
from astropy.io import fits

from astroskysim.cli import build_server
from astroskysim.config import Config, Sensor, SourceMode
from astroskysim.indi.xml_stream import XmlStreamSplitter, parse_element


class Client:
    """Minimal INDI client: send elements, collect parsed ones."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.splitter = XmlStreamSplitter()
        self.seen: list = []

    async def send(self, xml: str) -> None:
        self.writer.write(xml.encode())
        await self.writer.drain()

    async def pump(self, seconds: float = 1.0) -> None:
        """Read for a while, accumulating elements."""
        end = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < end:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout=0.1)
            except TimeoutError:
                continue
            if not chunk:
                return
            for raw in self.splitter.feed(chunk):
                self.seen.append(parse_element(raw))

    async def until(self, predicate, timeout: float = 8.0):
        """Pump until ``predicate(element)`` matches, and return that element."""
        end = asyncio.get_running_loop().time() + timeout
        for el in self.seen:
            if predicate(el):
                return el
        while asyncio.get_running_loop().time() < end:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), timeout=0.2)
            except TimeoutError:
                continue
            if not chunk:
                break
            for raw in self.splitter.feed(chunk):
                el = parse_element(raw)
                self.seen.append(el)
                if predicate(el):
                    return el
        raise AssertionError("timed out waiting for element")

    def mark(self) -> None:
        """Forget what has arrived so far.

        The devices push state continuously, so without this ``until`` can match
        a periodic update emitted *before* the command under test was sent."""
        self.seen.clear()

    def vectors(self, name: str) -> list:
        return [e for e in self.seen if e.name == name]

    async def close(self) -> None:
        self.writer.close()
        with __import__("contextlib").suppress(Exception):
            await self.writer.wait_closed()


def make_config(**over) -> Config:
    cfg = Config()
    cfg.server.port = 0  # ask the OS for a free port
    cfg.sensor.width_px = 160
    cfg.sensor.height_px = 120
    cfg.source.mode = SourceMode.ARTIFICIAL
    cfg.optics.seeing_arcsec = 3.0
    for k, v in over.items():
        obj = cfg
        *path, leaf = k.split(".")
        for p in path:
            obj = getattr(obj, p)
        setattr(obj, leaf, v)
    return cfg


def place(rig, ra_deg: float, dec_deg: float) -> None:
    """Put the mount at a position without going through sync.

    ``sync_to`` deliberately does *not* move the optics (it books a pointing
    correction instead), so it is no way to place a test rig.
    """
    m = rig.mount
    m.ra_deg = m.target_ra_deg = ra_deg
    m.dec_deg = m.target_dec_deg = dec_deg


class Harness:
    def __init__(self, cfg):
        self.cfg = cfg

    async def __aenter__(self):
        self.clients: list[Client] = []
        self.server = build_server(self.cfg)
        await self.server.start()
        self.port = self.server._server.sockets[0].getsockname()[1]
        self.tick = asyncio.create_task(self.server._tick(0.02))
        return self

    async def connect(self) -> Client:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        c = Client(reader, writer)
        self.clients.append(c)
        return c

    async def __aexit__(self, *exc):
        self.tick.cancel()
        for c in self.clients:
            await c.close()
        await self.server.stop()


async def test_get_properties_returns_all_six_devices():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.0)

        devices = {e.device for e in c.seen if e.tag.startswith("def")}
        assert devices == {
            "AstroSkySim Telescope",
            "AstroSkySim CCD",
            "AstroSkySim Guider",
            "AstroSkySim Focuser",
            "AstroSkySim Rotator",
            "AstroSkySim Filter Wheel",
        }
        await c.close()


async def test_driver_interface_bitmasks():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.0)

        got = {}
        for e in c.seen:
            if e.name == "DRIVER_INFO":
                got[e.device] = e.child_values()["DRIVER_INTERFACE"]
        # GUIDER (bit 2) is the ST4 "accepts timed guide pulses" bit, not a
        # "this is a guide camera" label. A client builds its guide-pulse device
        # list from it, so only devices that implement TELESCOPE_TIMED_GUIDE_*
        # may set it. Matches indi_simulator_telescope (5) and
        # indi_simulator_ccd (22, i.e. CCD|GUIDER|FILTER).
        assert got["AstroSkySim Telescope"] == "5"  # TELESCOPE | GUIDER
        assert got["AstroSkySim CCD"] == "2"  # CCD
        assert got["AstroSkySim Guider"] == "6"  # CCD | GUIDER
        assert got["AstroSkySim Focuser"] == "8"
        assert got["AstroSkySim Filter Wheel"] == "16"
        assert got["AstroSkySim Rotator"] == "4096"
        await c.close()


async def test_mount_gap_properties_are_present():
    """The full mount property set must be announced, not just the coordinates."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await c.pump(1.0)

        names = {e.name for e in c.seen if e.tag.startswith("def")}
        for expected in (
            "TELESCOPE_MOTION_NS",
            "TELESCOPE_MOTION_WE",
            "TELESCOPE_SLEW_RATE",
            "TELESCOPE_TRACK_RATE",
            "GUIDE_RATE",
            "TELESCOPE_OFFSET_RATES",
            "HORIZONTAL_COORD",
            "TIME_LST",
            "TELESCOPE_HOME",
            "TELESCOPE_PARK_POSITION",
            "TELESCOPE_PARK_OPTION",
            "CONFIG_PROCESS",
        ):
            assert expected in names, f"{expected} missing"
        await c.close()


async def test_slew_moves_and_settles():
    async with Harness(make_config(**{"mount.slew_rate_deg_s": 30.0})) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await c.pump(0.4)
        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim Telescope" name="EQUATORIAL_EOD_COORD">'
            '<oneNumber name="RA">5.5</oneNumber><oneNumber name="DEC">22.0</oneNumber>'
            "</newNumberVector>"
        )
        settled = await c.until(
            lambda e: e.tag == "setNumberVector"
            and e.name == "EQUATORIAL_EOD_COORD"
            and e.get("state") == "Ok"
        )
        vals = settled.child_values()
        assert float(vals["RA"]) == pytest.approx(5.5, abs=1e-3)
        assert float(vals["DEC"]) == pytest.approx(22.0, abs=1e-3)
        await c.close()


async def test_the_mount_status_is_reported_in_the_eq_coord_state():
    """Ekos reads tracking out of ``EQUATORIAL_EOD_COORD``'s state attribute.

    ``TELESCOPE_TRACK_STATE`` is a control, not the status display: Ekos maps
    the state of the coordinate property alone (Idle -> parked/idle, Ok ->
    tracking, Busy -> slewing). Reporting Ok whenever the mount is not slewing
    showed a parked mount as tracking, and put the indicator back to On a tick
    after the user pressed Off - which is exactly what was reported.
    """
    async with Harness(make_config(**{"mount.slew_rate_deg_s": 90.0})) as h:
        rig = h.server.rig
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await c.pump(0.4)

        async def eq_state(want: str):
            return await c.until(
                lambda e: e.tag == "setNumberVector"
                and e.name == "EQUATORIAL_EOD_COORD"
                and e.get("state") == want
            )

        # Tracking: Ok.
        assert rig.mount.tracking
        c.mark()
        await eq_state("Ok")

        # Off must stick, not be overwritten by the next periodic push.
        c.mark()
        await c.send(
            '<newSwitchVector device="AstroSkySim Telescope" name="TELESCOPE_TRACK_STATE">'
            '<oneSwitch name="TRACK_OFF">On</oneSwitch></newSwitchVector>'
        )
        await eq_state("Idle")
        await c.pump(2.5)
        assert not rig.mount.tracking
        states = [e.get("state") for e in c.vectors("EQUATORIAL_EOD_COORD")]
        assert "Ok" not in states, f"tracking came back on its own: {states}"

        # Park: still Idle once the slew finishes, and the switch says Off.
        c.mark()
        await c.send(
            '<newSwitchVector device="AstroSkySim Telescope" name="TELESCOPE_PARK">'
            '<oneSwitch name="PARK">On</oneSwitch></newSwitchVector>'
        )
        await c.until(
            lambda e: e.tag == "setSwitchVector"
            and e.name == "TELESCOPE_PARK"
            and e.get("state") == "Ok"
        )
        assert rig.mount.parked and not rig.mount.tracking
        c.mark()
        await c.pump(2.5)
        states = [e.get("state") for e in c.vectors("EQUATORIAL_EOD_COORD")]
        assert states and "Ok" not in states, f"a parked mount reported tracking: {states}"
        track = c.vectors("TELESCOPE_TRACK_STATE")
        if track:
            assert track[-1].child_values() == {"TRACK_ON": "Off", "TRACK_OFF": "On"}

        # A parked mount cannot be told to track.
        c.mark()
        await c.send(
            '<newSwitchVector device="AstroSkySim Telescope" name="TELESCOPE_TRACK_STATE">'
            '<oneSwitch name="TRACK_ON">On</oneSwitch></newSwitchVector>'
        )
        el = await c.until(
            lambda e: e.tag == "setSwitchVector" and e.name == "TELESCOPE_TRACK_STATE"
        )
        assert el.get("state") == "Alert"
        assert el.child_values()["TRACK_ON"] == "Off"
        assert not rig.mount.tracking
        await c.close()


async def test_sync_then_slew_removes_the_pointing_error():
    """Plate-solve-and-centre has to converge.

    Ekos' "Slew to target" syncs on the solved position and then slews to the
    target again. A sync that drags the optics along with the reported position
    leaves the error untouched through that round trip, so every iteration slews
    by the residual and then measures the same residual - which is what the
    pointing model in ``Rig.sync_to`` exists to prevent.
    """
    cfg = make_config(
        **{
            "mount.slew_rate_deg_s": 30.0,
            "mount.tracking_noise": 0.0,
            "mount.periodic_error_amplitude": 0.0,
            "mount.azimuth_error": 20.0,  # arcmin of polar misalignment
            "mount.elevation_error": 30.0,
        }
    )
    target_ra_h, target_dec = 2.5, 61.5

    def error_arcsec(pointing) -> float:
        dra = (pointing[0] - target_ra_h * 15.0) * np.cos(np.deg2rad(target_dec))
        return float(np.hypot(dra, pointing[1] - target_dec)) * 3600.0

    async with Harness(cfg) as h:
        rig = h.server.rig
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await c.pump(0.4)

        async def goto(action: str, ra_h: float, dec: float) -> None:
            await c.send(
                '<newSwitchVector device="AstroSkySim Telescope" name="ON_COORD_SET">'
                f'<oneSwitch name="{action}">On</oneSwitch></newSwitchVector>'
            )
            await c.pump(0.2)
            c.mark()
            await c.send(
                '<newNumberVector device="AstroSkySim Telescope" name="EQUATORIAL_EOD_COORD">'
                f'<oneNumber name="RA">{ra_h}</oneNumber>'
                f'<oneNumber name="DEC">{dec}</oneNumber></newNumberVector>'
            )
            wanted = "Busy" if action != "SYNC" else "Ok"
            await c.until(
                lambda e: e.tag == "setNumberVector"
                and e.name == "EQUATORIAL_EOD_COORD"
                and e.get("state") == wanted
            )
            if action != "SYNC":
                await c.until(
                    lambda e: e.tag == "setNumberVector"
                    and e.name == "EQUATORIAL_EOD_COORD"
                    and e.get("state") == "Ok"
                )

        await goto("TRACK", target_ra_h, target_dec)
        before = error_arcsec(rig.actual_pointing)
        assert before > 300, "the configured misalignment should miss by arcminutes"

        # One Ekos iteration: sync on the solved position, slew to the target.
        solved_ra, solved_dec = rig.actual_pointing
        await goto("SYNC", solved_ra / 15.0, solved_dec)
        # A sync moves the reported position only.
        assert error_arcsec(rig.actual_pointing) == pytest.approx(before, abs=1.0)
        await goto("TRACK", target_ra_h, target_dec)

        assert error_arcsec(rig.actual_pointing) < 30.0
        await c.close()


async def test_sexagesimal_write_is_accepted():
    async with Harness(make_config(**{"mount.slew_rate_deg_s": 90.0})) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await c.pump(0.4)
        # Select SYNC so the position is adopted immediately.
        await c.send(
            '<newSwitchVector device="AstroSkySim Telescope" name="ON_COORD_SET">'
            '<oneSwitch name="SYNC">On</oneSwitch></newSwitchVector>'
        )
        await c.pump(0.2)
        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim Telescope" name="EQUATORIAL_EOD_COORD">'
            '<oneNumber name="RA">05:30:00</oneNumber>'
            '<oneNumber name="DEC">-22:30:00</oneNumber></newNumberVector>'
        )
        el = await c.until(
            lambda e: e.tag == "setNumberVector"
            and e.name == "EQUATORIAL_EOD_COORD"
            and e.get("state") == "Ok"
        )
        assert float(el.child_values()["RA"]) == pytest.approx(5.5, abs=1e-3)
        assert float(el.child_values()["DEC"]) == pytest.approx(-22.5, abs=1e-3)
        await c.close()


async def test_blobs_are_withheld_until_enabled():
    """A client that never sends enableBLOB must not receive frames."""
    cfg = make_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.3)
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">0.05</oneNumber></newNumberVector>'
        )
        # Wait for the exposure to complete.
        await c.until(
            lambda e: e.name == "CCD_EXPOSURE"
            and e.tag == "setNumberVector"
            and e.get("state") == "Ok"
        )
        await c.pump(0.3)
        assert not [e for e in c.seen if e.tag == "setBLOBVector"]
        await c.close()


async def test_exposure_delivers_fits_at_sensor_size():
    cfg = make_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.3)
        await c.send('<enableBLOB device="AstroSkySim CCD">Also</enableBLOB>')
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">0.05</oneNumber></newNumberVector>'
        )
        blob = await c.until(lambda e: e.tag == "setBLOBVector")
        payload = blob.children[0]
        import base64

        data = base64.b64decode(payload.text)
        with fits.open(io.BytesIO(data)) as hdul:
            hdr = hdul[0].header
            img = hdul[0].data
        assert hdr["NAXIS1"] == cfg.sensor.width_px
        assert hdr["NAXIS2"] == cfg.sensor.height_px
        assert img.dtype == np.uint16
        # WCS and provenance keywords an imaging client expects to find.
        assert hdr["CTYPE1"] == "RA---TAN"
        assert "HFD" in hdr and "FOCUSPOS" in hdr
        assert hdr["ROWORDER"] == "BOTTOM-UP"
        await c.close()


async def test_compressed_blob_round_trips():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.3)
        await c.send('<enableBLOB device="AstroSkySim CCD">Also</enableBLOB>')
        await c.send(
            '<newSwitchVector device="AstroSkySim CCD" name="CCD_COMPRESSION">'
            '<oneSwitch name="CCD_COMPRESS">On</oneSwitch></newSwitchVector>'
        )
        await c.pump(0.2)
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">0.05</oneNumber></newNumberVector>'
        )
        blob = await c.until(lambda e: e.tag == "setBLOBVector")
        assert blob.children[0].get("format") == ".fits.z"
        import base64

        raw = zlib.decompress(base64.b64decode(blob.children[0].text))
        with fits.open(io.BytesIO(raw)) as hdul:
            assert hdul[0].data.shape == (120, 160)
        await c.close()


async def test_subframe_and_binning_change_frame_size():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.3)
        await c.send('<enableBLOB device="AstroSkySim CCD">Also</enableBLOB>')
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_FRAME">'
            '<oneNumber name="X">10</oneNumber><oneNumber name="Y">20</oneNumber>'
            '<oneNumber name="WIDTH">80</oneNumber><oneNumber name="HEIGHT">60</oneNumber>'
            "</newNumberVector>"
        )
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_BINNING">'
            '<oneNumber name="HOR_BIN">2</oneNumber><oneNumber name="VER_BIN">2</oneNumber>'
            "</newNumberVector>"
        )
        await c.pump(0.3)
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">0.05</oneNumber></newNumberVector>'
        )
        blob = await c.until(lambda e: e.tag == "setBLOBVector")
        import base64

        with fits.open(io.BytesIO(base64.b64decode(blob.children[0].text))) as hdul:
            assert hdul[0].data.shape == (30, 40)  # 60/2, 80/2
        await c.close()


async def test_focuser_move_reports_busy_then_ok():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Focuser"/>')
        await c.pump(0.3)
        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim Focuser" name="ABS_FOCUS_POSITION">'
            '<oneNumber name="FOCUS_ABSOLUTE_POSITION">16000</oneNumber></newNumberVector>'
        )
        el = await c.until(
            lambda e: e.name == "ABS_FOCUS_POSITION"
            and e.tag == "setNumberVector"
            and e.get("state") == "Ok"
        )
        assert float(el.child_values()["FOCUS_ABSOLUTE_POSITION"]) == pytest.approx(16000, abs=1)
        await c.close()


async def test_filter_focus_offsets_are_exposed():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Filter Wheel"/>')
        await c.pump(0.5)
        offs = [e for e in c.seen if e.name == "FILTER_FOCUS_OFFSET"]
        assert offs, "FILTER_FOCUS_OFFSET was never announced"
        vals = offs[0].child_values()
        assert float(vals["FILTER_OFFSET_5"]) == pytest.approx(120)
        await c.close()


async def test_two_clients_see_the_same_mount():
    """The shared-device design: one rig, not one per connection."""
    async with Harness(make_config(**{"mount.slew_rate_deg_s": 90.0})) as h:
        a = await h.connect()
        b = await h.connect()
        for c in (a, b):
            await c.send('<getProperties device="AstroSkySim Telescope"/>')
        await asyncio.gather(a.pump(0.4), b.pump(0.4))

        await a.send(
            '<newSwitchVector device="AstroSkySim Telescope" name="ON_COORD_SET">'
            '<oneSwitch name="SYNC">On</oneSwitch></newSwitchVector>'
        )
        await a.pump(0.2)
        await a.send(
            '<newNumberVector device="AstroSkySim Telescope" name="EQUATORIAL_EOD_COORD">'
            '<oneNumber name="RA">12.25</oneNumber><oneNumber name="DEC">-5.5</oneNumber>'
            "</newNumberVector>"
        )
        # Client B, which issued nothing, must observe A's slew.
        el = await b.until(
            lambda e: e.tag == "setNumberVector"
            and e.name == "EQUATORIAL_EOD_COORD"
            and abs(float(e.child_values().get("RA", 0)) - 12.25) < 1e-3
        )
        assert float(el.child_values()["DEC"]) == pytest.approx(-5.5, abs=1e-3)
        await a.close()
        await b.close()


async def test_device_filter_keeps_other_devices_off_the_connection():
    """A client sees only what it subscribed to.

    Regression: every ``set*Vector`` used to go to every connection regardless
    of its ``getProperties``, so a client that asked for one device got the
    whole rig - including values for devices it had no definition for. Plain
    clients tolerate that; ``indiserver`` chaining (``dev@host:port``, one
    device per connection) does not.
    """
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(1.0)

        # The mount pushes on every tick, so a leak shows up well inside 1 s.
        assert c.seen, "no traffic at all"
        assert {e.device for e in c.seen} == {"AstroSkySim CCD"}
        await c.close()


async def test_property_filter_narrows_to_one_property():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send(
            '<getProperties device="AstroSkySim Telescope" name="EQUATORIAL_EOD_COORD"/>'
        )
        await c.pump(1.0)

        assert {e.name for e in c.seen} == {"EQUATORIAL_EOD_COORD"}
        assert [e for e in c.seen if e.tag.startswith("def")], "definition missing"
        await c.close()


async def test_unknown_device_name_yields_nothing():
    """The failure mode that broke a real KStars profile: a mistyped device
    name got no definitions but a full stream of values anyway, so the
    misconfiguration looked like a server bug."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Camera"/>')
        await c.pump(1.0)

        assert c.seen == []
        await c.close()


async def test_a_client_that_asked_for_nothing_gets_nothing():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.pump(1.0)
        assert c.seen == []
        await c.close()


async def test_set_is_never_sent_before_its_definition():
    """Whatever a client receives values for, it must first have a definition
    for - otherwise it has nothing to apply them to."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.5)

        defined: set = set()
        for e in c.seen:
            if e.tag.startswith("def"):
                defined.add((e.device, e.name))
            elif e.tag.startswith("set"):
                assert (e.device, e.name) in defined, f"set before def: {e.device}.{e.name}"
        await c.close()


async def test_a_property_that_appears_late_is_defined_before_its_value():
    """A property can become visible after a client's ``getProperties``. The
    client has no definition for it, so the value alone would be unusable -
    the definition must be sent ahead of it rather than the update dropped."""
    async with Harness(make_config()) as h:
        rotator = h.server.find("AstroSkySim Rotator")
        vec = rotator.vectors["CONFIG_PROCESS"]
        vec.enabled = False

        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Rotator"/>')
        await c.pump(0.5)
        assert not [e for e in c.seen if e.name == "CONFIG_PROCESS"]

        c.mark()
        vec.enabled = True
        rotator.push(vec)

        el = await c.until(lambda e: e.name == "CONFIG_PROCESS")
        assert el.tag == "defSwitchVector", f"value arrived first, as <{el.tag}>"
        await c.close()


async def test_read_only_property_write_is_refused():
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.3)
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_INFO">'
            '<oneNumber name="CCD_MAX_X">99</oneNumber></newNumberVector>'
        )
        el = await c.until(lambda e: e.tag == "message" and "read only" in e.get("message", ""))
        assert "CCD_INFO" in el.get("message")
        await c.close()


async def test_garbage_does_not_kill_the_server():
    """Junk between elements, unknown elements and malformed complete elements
    must all be survivable."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send("not xml at all   ")
        await c.send('<whoIsThis attr="1"/>')
        await c.send('<newNumberVector device="Nonexistent" name="X">'
                     '<oneNumber name="A">1</oneNumber></newNumberVector>')
        await c.send('<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
                     '<oneNumber name="CCD_EXPOSURE_VALUE">not-a-number</oneNumber>'
                     '</newNumberVector>')
        await c.send("<mismatched></nope>")
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.0)
        assert [e for e in c.seen if e.tag.startswith("def")], "server stopped responding"
        await c.close()


async def test_unterminated_element_blocks_that_connection_only():
    """A client that sends an element it never closes cannot be resynchronised -
    the bytes after it are, by definition, that element's content. Documented
    limitation: it must not affect other clients or the server."""
    async with Harness(make_config()) as h:
        bad = await h.connect()
        good = await h.connect()
        await bad.send('<newNumberVector device="x" name="y">')
        await bad.send('<getProperties version="1.7"/>')
        await good.send('<getProperties version="1.7"/>')
        await good.pump(1.0)
        assert [e for e in good.seen if e.tag.startswith("def")]
        await bad.close()
        await good.close()


# --------------------------------------------------------------------------
# Guiding.
#
# Ekos rejected calibration with "star drift is too short" because the ST4 bit
# and the pulse properties had drifted apart: the mount implemented
# TELESCOPE_TIMED_GUIDE_* but did not advertise GUIDER, so it was never offered
# as a pulse target, while the guide camera advertised GUIDER and implemented
# nothing. Pulses went nowhere and the field never moved.
# --------------------------------------------------------------------------
GUIDER_INTERFACE_BIT = 1 << 2


async def test_every_guider_interface_device_accepts_timed_pulses():
    """The invariant that ties the ST4 bit to the properties it promises."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.5)

        interfaces, props = {}, {}
        for e in c.seen:
            if e.name == "DRIVER_INFO":
                interfaces[e.device] = int(e.child_values()["DRIVER_INTERFACE"])
            if e.tag.startswith("def"):
                props.setdefault(e.device, set()).add(e.name)

        claiming = [d for d, i in interfaces.items() if i & GUIDER_INTERFACE_BIT]
        assert claiming, "no device advertises the ST4 guider interface"
        for device in claiming:
            assert "TELESCOPE_TIMED_GUIDE_NS" in props[device], device
            assert "TELESCOPE_TIMED_GUIDE_WE" in props[device], device
        await c.close()


@pytest.mark.parametrize("device", ["AstroSkySim Telescope", "AstroSkySim Guider"])
async def test_guide_pulse_moves_the_mount_from_either_st4_device(device):
    """Whichever device the client pulses, the one shared mount has to move."""
    cfg = make_config(**{"mount.guide_rate": 0.5, "mount.tracking_noise": 0.0})
    async with Harness(cfg) as h:
        rig = h.server.rig
        place(rig, 80.0, 20.0)
        before = rig.mount.dec_deg

        c = await h.connect()
        await c.send(f'<getProperties device="{device}"/>')
        await c.pump(0.4)
        c.mark()
        await c.send(
            f'<newNumberVector device="{device}" name="TELESCOPE_TIMED_GUIDE_NS">'
            '<oneNumber name="TIMED_GUIDE_N">1000</oneNumber>'
            '<oneNumber name="TIMED_GUIDE_S">0</oneNumber></newNumberVector>'
        )
        # The pulse must report Busy and then release, so the client is not
        # left waiting on it.
        await c.until(
            lambda e: e.name == "TELESCOPE_TIMED_GUIDE_NS" and e.get("state") == "Busy"
        )
        await c.until(
            lambda e: e.name == "TELESCOPE_TIMED_GUIDE_NS" and e.get("state") == "Ok"
        )
        # 1000 ms at 0.5x sidereal = 7.52 arcsec north.
        moved = (rig.mount.dec_deg - before) * 3600.0
        assert moved == pytest.approx(7.52, abs=0.2)
        await c.close()


async def test_guide_pulses_move_the_field_enough_to_calibrate():
    """A calibration-sized pulse train has to shift the guide frame linearly.

    Ekos calibrates by pulsing and measuring the shift in pixels; if a 1 s pulse
    does not move the field by pixels, calibration is rejected.
    """
    cfg = make_config(
        **{
            "mount.guide_rate": 0.5,
            "mount.tracking_noise": 0.0,
            "mount.periodic_error_amplitude": 0.0,
            "telescope.focal_length_mm": 700.0,
            "sensor.pixel_size_um": 5.0,
        }
    )
    async with Harness(cfg) as h:
        rig = h.server.rig
        place(rig, 80.0, 20.0)
        # A fixed sky point, tracked through the WCS as the mount is pulsed.
        w0 = rig.build_wcs(160, 120)
        star = w0.all_pix2world([[80.0, 60.0]], 0)[0]

        offsets = []
        for _ in range(4):
            rig.mount.guide_ns_s, rig.mount.guide_ns_sign = 1.0, 1
            while rig.mount.guide_ns_s > 0:
                await rig.step(0.1)
            x, y = rig.build_wcs(160, 120).all_world2pix([star], 0)[0]
            offsets.append(float(np.hypot(x - 80.0, y - 60.0)))

        step_px = 7.52 / cfg.scale_arcsec_px  # 7.52 arcsec per 1 s pulse
        assert offsets[0] == pytest.approx(step_px, rel=0.02)
        # Linear in the number of pulses, and comfortably measurable.
        assert offsets[-1] == pytest.approx(4 * step_px, rel=0.02)
        assert offsets[-1] > 5.0


async def test_hot_pixels_do_not_outshine_stars_in_a_short_exposure():
    """Hot pixels are dark current, so they must scale with exposure time.

    Fixed-pattern pixels brighter than every real star are what a guider locks
    onto, and they never move - so calibration measures no drift.
    """
    cfg = make_config(**{"sensor.hot_pixels": 20, "mount.tracking_noise": 0.0})
    async with Harness(cfg) as h:
        rig = h.server.rig
        guider = rig.guider
        guider.frame_type = 2  # dark: no stars, no sky, so only hot pixels remain

        guider.exposure_s = 0.5
        short = rig.capture(guider).astype(float)
        guider.exposure_s = 120.0
        long = rig.capture(guider).astype(float)

        gs = cfg.guide_sensor
        full_well = gs.well_depth_e / gs.e_per_adu
        # A 0.5 s guide frame must not contain saturated fixed-pattern pixels;
        # they would be brighter than any real guide star and never move.
        assert short.max() < 0.1 * full_well, "hot pixels saturate a 0.5 s frame"
        # Over a long light frame they do build up, as real hot pixels do.
        assert long.max() > 0.9 * full_well


# --- the guide camera is separate hardware ---------------------------------
# Sharing one [sensor] between both cameras made the guider a clone of the
# imaging chip: same size, same plate scale, same aperture. A client then sizes
# its guide star search box and its calibration from the wrong numbers, and no
# realistic guiding setup is reproducible.

GUIDE_SENSOR = Sensor(
    width_px=200,
    height_px=140,
    pixel_size_um=2.9,
    well_depth_e=10000.0,
    read_noise_e=3.5,
    hot_pixels=4,
)


def guide_config(**over) -> Config:
    return make_config(
        sensor_guide_cam=GUIDE_SENSOR.model_copy(),
        **{
            "telescope.guide_focal_length_mm": 240.0,
            "telescope.guide_aperture_mm": 60.0,
            "optics.guide_hfd_px": 3.5,
            **over,
        },
    )


async def read_frame(c: Client, device: str, exposure: float = 0.05):
    """Expose once on `device` and return the parsed FITS HDU."""
    import base64

    await c.send(f'<getProperties device="{device}"/>')
    await c.pump(0.3)
    await c.send(f'<enableBLOB device="{device}">Also</enableBLOB>')
    c.mark()
    await c.send(
        f'<newNumberVector device="{device}" name="CCD_EXPOSURE">'
        f'<oneNumber name="CCD_EXPOSURE_VALUE">{exposure}</oneNumber></newNumberVector>'
    )
    blob = await c.until(lambda e: e.tag == "setBLOBVector" and e.get("device") == device)
    data = base64.b64decode(blob.children[0].text)
    with fits.open(io.BytesIO(data)) as hdul:
        return hdul[0].header.copy(), hdul[0].data.copy()


def numbers(el) -> dict[str, float]:
    return {ch.get("name"): float(ch.text) for ch in el.children}


def header_scale_arcsec_px(hdr) -> float:
    """Plate scale as a client would read it back out of the header."""
    from astropy.wcs import WCS
    from astropy.wcs.utils import proj_plane_pixel_scales

    return float(proj_plane_pixel_scales(WCS(hdr))[0]) * 3600.0


async def test_guide_camera_announces_its_own_chip():
    cfg = guide_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        await c.send("<getProperties/>")
        await c.pump(0.6)
        info = {
            e.get("device"): numbers(e)
            for e in c.seen
            if e.name == "CCD_INFO" and e.tag == "defNumberVector"
        }
        main, guide = info["AstroSkySim CCD"], info["AstroSkySim Guider"]
        assert (main["CCD_MAX_X"], main["CCD_MAX_Y"]) == (cfg.sensor.width_px, cfg.sensor.height_px)
        assert (guide["CCD_MAX_X"], guide["CCD_MAX_Y"]) == (
            GUIDE_SENSOR.width_px,
            GUIDE_SENSOR.height_px,
        )
        assert guide["CCD_PIXEL_SIZE"] == GUIDE_SENSOR.pixel_size_um != main["CCD_PIXEL_SIZE"]
        await c.close()


async def test_guide_frame_has_the_guide_scope_geometry():
    """Pixel count from the guide chip, plate scale from the guide scope."""
    cfg = guide_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        hdr, img = await read_frame(c, "AstroSkySim Guider")
        assert (hdr["NAXIS1"], hdr["NAXIS2"]) == (GUIDE_SENSOR.width_px, GUIDE_SENSOR.height_px)
        assert img.shape == (GUIDE_SENSOR.height_px, GUIDE_SENSOR.width_px)
        assert hdr["FOCALLEN"] == 240.0
        assert hdr["APTDIA"] == 60.0
        assert hdr["XPIXSZ"] == pytest.approx(GUIDE_SENSOR.pixel_size_um)
        # 2.9 um through 240 mm is 2.49"/px, not the imaging scale.
        scale = header_scale_arcsec_px(hdr)
        assert scale == pytest.approx(cfg.guide_scale_arcsec_px, rel=1e-6)
        assert scale != pytest.approx(cfg.scale_arcsec_px, rel=1e-3)
        await c.close()


async def test_imaging_frame_keeps_the_main_scope_geometry():
    """The guide camera's specs must not leak into the imaging camera."""
    cfg = guide_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        hdr, img = await read_frame(c, "AstroSkySim CCD")
        assert (hdr["NAXIS1"], hdr["NAXIS2"]) == (cfg.sensor.width_px, cfg.sensor.height_px)
        assert hdr["FOCALLEN"] == cfg.telescope.focal_length_mm
        scale = header_scale_arcsec_px(hdr)
        assert scale == pytest.approx(cfg.scale_arcsec_px, rel=1e-6)
        await c.close()


async def test_mount_reports_the_guide_scope_in_telescope_info():
    """GUIDER_FOCAL_LENGTH exists on the wire; it used to echo the main scope."""
    cfg = guide_config()
    async with Harness(cfg) as h:
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Telescope"/>')
        el = await c.until(lambda e: e.name == "TELESCOPE_INFO")
        v = numbers(el)
        assert v["TELESCOPE_FOCAL_LENGTH"] == cfg.telescope.focal_length_mm
        assert v["GUIDER_FOCAL_LENGTH"] == 240.0
        assert v["GUIDER_APERTURE"] == 60.0
        await c.close()


async def test_a_focus_run_does_not_blur_a_fixed_focus_guide_star():
    """A separate guide scope holds its own focus.

    If the imaging focuser drove the guide camera too, an autofocus sweep would
    bloat the guide star to HFD 10 and the guiding loop would lose it - which is
    not something a real rig with a guide scope does.
    """
    cfg = guide_config()
    async with Harness(cfg) as h:
        rig = h.server.rig
        rig.focuser.position = cfg.focuser.perfect_focus + 3 * cfg.focuser.focus_range
        assert rig.current_hfd() > 20.0, "the imaging camera must go out of focus"
        assert rig.guide_hfd() == pytest.approx(3.5)


async def test_an_off_axis_guider_follows_the_focuser():
    """The default rig: the OAG prism is downstream of the focuser.

    An autofocus run therefore does blur the guide star, which is why Ekos
    suspends guiding for one. Pinning the guide HFD here would hide that.
    """
    cfg = make_config()
    async with Harness(cfg) as h:
        rig = h.server.rig
        rig.focuser.position = cfg.focuser.perfect_focus + 2 * cfg.focuser.focus_range
        assert rig.guide_hfd() == pytest.approx(rig.current_hfd())
        assert rig.guide_hfd() > 10.0


async def test_a_filter_focus_offset_softens_the_oag_guide_star():
    """The OAG prism is upstream of the filter wheel.

    It has to be, or a narrowband filter would starve the guide camera. So the
    offset that brings the imaging chip into focus on Ha takes the guide star the
    same distance out of focus - the imaging HFD is best while the guide HFD is
    not, and the two must not be computed from the same perfect-focus position.
    """
    cfg = make_config(**{"filter_wheel.focus_offsets": [0, 0, 0, 0, 120]})
    ha_slot = len(cfg.filter_wheel.names)  # Ha, the offset filter
    async with Harness(cfg) as h:
        rig = h.server.rig
        rig.filter.slot = rig.filter.target = ha_slot
        # Focused for Ha: the imaging chip is sharp, the guide star is 120 steps out.
        rig.focuser.position = cfg.focuser.perfect_focus + 120
        assert rig.current_hfd() == pytest.approx(2.35, abs=0.01)
        assert rig.guide_hfd() > rig.current_hfd()
        # Small, but the whole point is that it is not zero.
        assert rig.guide_hfd() == pytest.approx(2.64, abs=0.05)


async def test_without_a_guide_sensor_section_both_cameras_match():
    """Backward compatibility: existing configs behave exactly as before."""
    cfg = make_config()
    assert cfg.guide_sensor is cfg.sensor
    assert cfg.guide_scale_arcsec_px == cfg.scale_arcsec_px
    async with Harness(cfg) as h:
        c = await h.connect()
        await c.send("<getProperties/>")
        await c.pump(0.6)
        info = {
            e.get("device"): numbers(e)
            for e in c.seen
            if e.name == "CCD_INFO" and e.tag == "defNumberVector"
        }
        assert info["AstroSkySim CCD"] == info["AstroSkySim Guider"]
        await c.close()


class SlowSource:
    """Wraps a source and makes rendering take real time.

    Stands in for a survey reprojection, which costs ~0.6 s on a guide chip and
    ~3 s on a 3008x3008 imaging chip.
    """

    name = "slow"

    def __init__(self, inner, delay_s: float) -> None:
        self.inner = inner
        self.delay_s = delay_s

    def render(self, ctx):
        time.sleep(self.delay_s)
        return self.inner.render(ctx)


async def test_a_slow_readout_neither_stops_the_clock_nor_blocks_a_client():
    """Rendering a frame must not freeze the simulation or the connection.

    ``rig.capture`` used to run inline in the coroutine ``IndiServer._tick``
    awaits, so a slow render froze every device for its whole duration: the
    mount stopped tracking, property updates stopped going out, and a guide
    pulse sat unread in the socket until the frame was done. On top of that the
    tick stepped by its nominal period rather than by elapsed time, so the
    blocked seconds were deleted from the simulated clock instead of being
    caught up. Composite mode measured 40% of real time with 3.3 s stalls -
    guiding cannot survive corrections arriving three frames late.
    """
    async with Harness(make_config()) as h:
        rig = h.server.rig
        rig.source = SlowSource(rig.source, 0.6)
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(0.4)
        await c.send('<enableBLOB device="AstroSkySim CCD">Also</enableBLOB>')
        c.mark()

        real0, sim0 = time.perf_counter(), rig.elapsed_s
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">0.05</oneNumber></newNumberVector>'
        )
        # A guide pulse issued while the frame is rendering must be answered
        # during the render, not after it.
        await c.send(
            '<newNumberVector device="AstroSkySim Telescope" '
            'name="TELESCOPE_TIMED_GUIDE_WE">'
            '<oneNumber name="TIMED_GUIDE_W">50</oneNumber></newNumberVector>'
        )
        pulse = await c.until(
            lambda e: e.name == "TELESCOPE_TIMED_GUIDE_WE"
            and e.tag == "setNumberVector"
        )
        assert pulse is not None
        pulse_at = time.perf_counter() - real0
        assert pulse_at < 0.5, f"guide pulse answered only after {pulse_at:.2f} s"

        await c.until(lambda e: e.tag == "setBLOBVector")
        real = time.perf_counter() - real0
        sim = rig.elapsed_s - sim0
        # The slow render really happened...
        assert real > 0.6, real
        # ...and the simulated clock kept up with wall clock through it.
        assert sim > 0.85 * real, f"simulated {sim:.2f} s of {real:.2f} s real"
        await c.close()


# --------------------------------------------------------------------------
# Weather.
#
# The wind is fully simulated whether or not this device exists - the guide star
# moves and a client's RMS spikes either way. What the device adds is the ability
# for a client to *react*, and the property a client reads for that is
# WEATHER_STATUS's vector state, not the readings in WEATHER_PARAMETERS. A driver
# that publishes numbers and no status is decoration.
#
# Off by default, because a client's profile enumerates devices and an
# unexpected seventh one turns up in every existing Ekos profile.
# --------------------------------------------------------------------------
def windy_config(**over):
    cfg = make_config(**over)
    cfg.server.weather = True
    cfg.wind = cfg.wind.model_copy(
        update=dict(
            enabled=True,
            speed_kmh=25.0,
            probability=0.95,
            gust_speed_kmh=70.0,
            gust_probability=0.3,
        )
    )
    return cfg


async def test_the_weather_device_is_absent_unless_asked_for():
    """Default off, so no existing profile grows a device."""
    async with Harness(make_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.0)
        devices = {e.device for e in c.seen if e.tag.startswith("def")}
        assert "AstroSkySim Weather" not in devices
        await c.close()


async def test_the_weather_device_appears_and_reports_the_wind():
    async with Harness(windy_config()) as h:
        rig = h.server.rig
        rig.wind.windy = True
        rig.wind.speed_kmh = 25.0

        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Weather"/>')
        await c.pump(0.5)

        defs = {e.name for e in c.seen if e.tag.startswith("def")}
        assert {"WEATHER_PARAMETERS", "WEATHER_STATUS", "WEATHER_UPDATE"} <= defs

        got = {}
        for e in c.seen:
            if e.name == "DRIVER_INFO":
                got[e.device] = e.child_values()["DRIVER_INTERFACE"]
        # WEATHER is bit 7 in indiapi.h. Pinned here because the per-key style of
        # test_driver_interface_bitmasks means a wrong value would not fail it.
        assert got["AstroSkySim Weather"] == "128"

        c.mark()
        params = await c.until(
            lambda e: e.name == "WEATHER_PARAMETERS" and e.tag == "setNumberVector"
        )
        assert float(params.child_values()["WEATHER_WIND_SPEED"]) > 0.0
        await c.close()


async def test_the_weather_device_does_not_claim_the_st4_bit():
    """It must not, or the guider-interface invariant would demand pulses of it.

    Exactly the mistake a copy-paste from mount.py makes.
    """
    async with Harness(windy_config()) as h:
        c = await h.connect()
        await c.send('<getProperties version="1.7"/>')
        await c.pump(1.5)
        for e in c.seen:
            if e.name == "DRIVER_INFO" and e.device == "AstroSkySim Weather":
                assert not int(e.child_values()["DRIVER_INTERFACE"]) & GUIDER_INTERFACE_BIT
        await c.close()


async def test_wind_past_the_threshold_raises_an_alert():
    """The state a scheduler reads to decide whether to keep imaging."""
    async with Harness(windy_config()) as h:
        rig = h.server.rig
        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim Weather"/>')
        await c.pump(0.5)

        # A limit this rig will exceed, then one it cannot.
        rig.wind.windy, rig.wind.gusting = True, False
        rig.wind.speed_kmh = 30.0
        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim Weather" name="WEATHER_WIND_SPEED">'
            '<oneNumber name="MIN_OK">0</oneNumber>'
            '<oneNumber name="MAX_OK">10</oneNumber>'
            '<oneNumber name="PERCENT_WARNING">15</oneNumber></newNumberVector>'
        )
        alert = await c.until(
            lambda e: e.name == "WEATHER_STATUS" and e.get("state") == "Alert"
        )
        assert alert is not None

        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim Weather" name="WEATHER_WIND_SPEED">'
            '<oneNumber name="MIN_OK">0</oneNumber>'
            '<oneNumber name="MAX_OK">200</oneNumber>'
            '<oneNumber name="PERCENT_WARNING">15</oneNumber></newNumberVector>'
        )
        ok = await c.until(lambda e: e.name == "WEATHER_STATUS" and e.get("state") == "Ok")
        assert ok is not None
        await c.close()


async def test_a_wind_smeared_sub_records_it_in_the_header():
    """Ground truth, for the same reason NSATS exists.

    A client looking at streaked stars cannot otherwise tell wind from a bad
    guide star or a slipped clutch.
    """
    cfg = windy_config(**{"telescope.focal_length_mm": 2000.0})
    cfg.wind = cfg.wind.model_copy(update=dict(response_arcsec_at_20kmh=4.0))
    async with Harness(cfg) as h:
        rig = h.server.rig
        rig.wind.windy = True
        rig.wind.speed_kmh = 25.0
        # Give the model a history to smear over before the shutter opens.
        for _ in range(400):
            rig.wind.step(0.02)

        c = await h.connect()
        await c.send('<getProperties device="AstroSkySim CCD"/>')
        await c.pump(0.4)
        await c.send('<enableBLOB device="AstroSkySim CCD">Also</enableBLOB>')
        c.mark()
        await c.send(
            '<newNumberVector device="AstroSkySim CCD" name="CCD_EXPOSURE">'
            '<oneNumber name="CCD_EXPOSURE_VALUE">1.0</oneNumber></newNumberVector>'
        )
        blob = await c.until(lambda e: e.tag == "setBLOBVector")
        import base64

        with fits.open(io.BytesIO(base64.b64decode(blob.children[0].text))) as hdul:
            header = hdul[0].header
            assert header["WINDKMH"] > 0.0
            assert "GUSTKMH" in header
            assert "SMEARPX" in header
        await c.close()
