"""CCD cameras.

The behaviour lives once in ``CameraBase``. The two concrete cameras differ
only in name, interface bit and which ``CameraState`` on the rig they drive —
but they are different *hardware*, so neither may read the other's sensor
config; ask the rig (``rig.sensor_cfg``, ``rig.build_optics``) instead.
"""

from __future__ import annotations

import asyncio
import io
import logging
import zlib

import numpy as np
from astropy.io import fits

from ..indi.device import CCD_INTERFACE, GUIDER_INTERFACE, Device
from ..indi.protocol import (
    BlobItem,
    BlobVector,
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
from ..rig import CameraState
from ..sky.wcs import frame_wcs
from .pulse import GuidePulseMixin

log = logging.getLogger("astroskysim.devices.camera")

FRAME_TYPES = ("FRAME_LIGHT", "FRAME_BIAS", "FRAME_DARK", "FRAME_FLAT")
IMAGETYP = ("Light", "Bias", "Dark", "Flat")


class CameraBase(Device):
    interface = CCD_INTERFACE
    #: Attribute on the rig holding this camera's state.
    state_attr = "camera"
    blob_name = "CCD1"

    @property
    def cam(self) -> CameraState:
        return getattr(self.rig, self.state_attr)

    @property
    def sensor(self):
        """This camera's own chip - the guider's is not the imaging one."""
        return self.rig.sensor_cfg(self.cam)

    def setup(self) -> None:
        #: In-flight readouts, held so the tasks are not garbage-collected.
        self._readout_tasks: set[asyncio.Task] = set()
        s = self.sensor
        cam = self.cam

        self.exposure = self.add(
            NumberVector(
                name="CCD_EXPOSURE",
                label="Expose",
                items=[
                    NumberItem("CCD_EXPOSURE_VALUE", "Duration (s)", 1.0, "%.3f", 0.0, 3600.0, 0.001)
                ],
            )
        )
        self.abort = self.add(
            SwitchVector(
                name="CCD_ABORT_EXPOSURE",
                label="Abort",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("ABORT", "Abort")],
            )
        )
        # Stop ends the exposure early and still delivers the frame, unlike
        # abort, which discards it. Clients use the two differently.
        self.stop = self.add(
            SwitchVector(
                name="CCD_STOP_EXPOSURE",
                label="Stop (keep frame)",
                rule=SwitchRule.AT_MOST_ONE,
                items=[SwitchItem("STOP", "Stop")],
            )
        )
        self.info = self.add(
            NumberVector(
                name="CCD_INFO",
                label="CCD Information",
                group="Image Info",
                perm=Perm.RO,
                items=[
                    NumberItem("CCD_MAX_X", "Max X", s.width_px, "%.0f", 1, 1e5, 1),
                    NumberItem("CCD_MAX_Y", "Max Y", s.height_px, "%.0f", 1, 1e5, 1),
                    NumberItem("CCD_PIXEL_SIZE", "Pixel (um)", s.pixel_size_um, "%.2f", 0, 100, 0),
                    NumberItem("CCD_PIXEL_SIZE_X", "Pixel X (um)", s.pixel_size_um, "%.2f", 0, 100, 0),
                    NumberItem("CCD_PIXEL_SIZE_Y", "Pixel Y (um)", s.pixel_size_um, "%.2f", 0, 100, 0),
                    NumberItem("CCD_BITSPERPIXEL", "Bits/pixel", s.bit_depth, "%.0f", 8, 16, 0),
                ],
            )
        )
        self.frame = self.add(
            NumberVector(
                name="CCD_FRAME",
                label="Frame",
                group="Image Settings",
                items=[
                    NumberItem("X", "Left", 0, "%.0f", 0, s.width_px - 1, 1),
                    NumberItem("Y", "Top", 0, "%.0f", 0, s.height_px - 1, 1),
                    NumberItem("WIDTH", "Width", s.width_px, "%.0f", 1, s.width_px, 1),
                    NumberItem("HEIGHT", "Height", s.height_px, "%.0f", 1, s.height_px, 1),
                ],
            )
        )
        self.binning = self.add(
            NumberVector(
                name="CCD_BINNING",
                label="Binning",
                group="Image Settings",
                items=[
                    NumberItem("HOR_BIN", "X", 1, "%.0f", 1, 4, 1),
                    NumberItem("VER_BIN", "Y", 1, "%.0f", 1, 4, 1),
                ],
            )
        )
        self.frame_type = self.add(
            SwitchVector(
                name="CCD_FRAME_TYPE",
                label="Frame Type",
                group="Image Settings",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("FRAME_LIGHT", "Light", True),
                    SwitchItem("FRAME_BIAS", "Bias"),
                    SwitchItem("FRAME_DARK", "Dark"),
                    SwitchItem("FRAME_FLAT", "Flat"),
                ],
            )
        )
        self.gain = self.add(
            NumberVector(
                name="CCD_GAIN",
                label="Gain",
                group="Image Settings",
                items=[NumberItem("GAIN", "Gain", cam.gain, "%.0f", 0, 1000, 1)],
            )
        )
        self.offset = self.add(
            NumberVector(
                name="CCD_OFFSET",
                label="Offset",
                group="Image Settings",
                items=[NumberItem("OFFSET", "Offset", cam.offset, "%.0f", 0, 2000, 1)],
            )
        )
        self.temperature = self.add(
            NumberVector(
                name="CCD_TEMPERATURE",
                label="Temperature",
                group="Main Control",
                items=[
                    NumberItem("CCD_TEMPERATURE_VALUE", "Temp (C)", cam.temperature, "%.1f", -50, 50, 0.1)
                ],
            )
        )
        self.cooler = self.add(
            SwitchVector(
                name="CCD_COOLER",
                label="Cooler",
                group="Main Control",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("COOLER_ON", "On", False),
                    SwitchItem("COOLER_OFF", "Off", True),
                ],
            )
        )
        # Cooler duty cycle, so a client can watch it settle after a setpoint change.
        self.cooler_power = self.add(
            NumberVector(
                name="CCD_COOLER_POWER",
                label="Cooler Power",
                group="Main Control",
                perm=Perm.RO,
                items=[NumberItem("CCD_COOLER_VALUE", "Power (%)", 0, "%.0f", 0, 100, 1)],
            )
        )
        # Readout mode selection.
        self.readout_mode = self.add(
            SwitchVector(
                name="CCD_READOUT_MODE",
                label="Readout Mode",
                group="Image Settings",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("READOUT_NORMAL", "Normal", True),
                    SwitchItem("READOUT_FAST", "Fast"),
                ],
            )
        )
        self.cfa = self.add(
            TextVector(
                name="CCD_CFA",
                label="Bayer",
                group="Image Info",
                perm=Perm.RO,
                items=[
                    TextItem("CFA_OFFSET_X", "Offset X", "0"),
                    TextItem("CFA_OFFSET_Y", "Offset Y", "0"),
                    TextItem("CFA_TYPE", "Pattern", "" if s.bayer == "MONO" else s.bayer),
                ],
            )
        )
        self.compression = self.add(
            SwitchVector(
                name="CCD_COMPRESSION",
                label="Compression",
                group="Image Settings",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("CCD_COMPRESS", "Compress"),
                    SwitchItem("CCD_RAW", "Raw", True),
                ],
            )
        )
        self.upload_mode = self.add(
            SwitchVector(
                name="UPLOAD_MODE",
                label="Upload",
                group="Options",
                rule=SwitchRule.ONE_OF_MANY,
                items=[
                    SwitchItem("UPLOAD_CLIENT", "Client", True),
                    SwitchItem("UPLOAD_LOCAL", "Local"),
                    SwitchItem("UPLOAD_BOTH", "Both"),
                ],
            )
        )
        self.ccd_blob = self.add(
            BlobVector(
                name=self.blob_name,
                label="Image",
                group="Image Info",
                items=[BlobItem(self.blob_name, "Frame")],
            )
        )

        for name, fn in (
            ("CCD_EXPOSURE", self._w_exposure),
            ("CCD_ABORT_EXPOSURE", self._w_abort),
            ("CCD_STOP_EXPOSURE", self._w_stop),
            ("CCD_FRAME", self._w_frame),
            ("CCD_BINNING", self._w_binning),
            ("CCD_FRAME_TYPE", self._w_frame_type),
            ("CCD_GAIN", self._w_gain),
            ("CCD_OFFSET", self._w_offset),
            ("CCD_TEMPERATURE", self._w_temperature),
            ("CCD_COOLER", self._w_cooler),
            ("CCD_READOUT_MODE", self._w_switch_ok),
            ("CCD_COMPRESSION", self._w_switch_ok),
            ("UPLOAD_MODE", self._w_switch_ok),
        ):
            self.writer(name, fn)

    # -- writes ------------------------------------------------------------
    async def _w_switch_ok(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)

    async def _w_exposure(self, vec: Vector, values: dict[str, str]) -> None:
        secs = parse_number(values.get("CCD_EXPOSURE_VALUE", "0"))
        cam = self.cam
        cam.exposure_s = max(secs, 0.0)
        cam.remaining_s = cam.exposure_s
        cam.exposing = True
        cam.aborted = False
        cam.last_start_time = self.rig.iso_utc
        cam.start_jd = self.rig.jd
        vec["CCD_EXPOSURE_VALUE"].value = cam.remaining_s
        self.push(vec, state=PropState.BUSY)
        if cam.exposure_s <= 0:
            cam.exposing = False
            self._spawn_readout()

    async def _w_abort(self, vec: Vector, values: dict[str, str]) -> None:
        cam = self.cam
        cam.exposing = False
        cam.aborted = True
        cam.remaining_s = 0.0
        for it in vec.items:
            it.value = False
        self.exposure["CCD_EXPOSURE_VALUE"].value = 0.0
        self.push(self.exposure, state=PropState.IDLE)
        self.push(vec, state=PropState.OK, message="exposure aborted, frame discarded")

    async def _w_stop(self, vec: Vector, values: dict[str, str]) -> None:
        """Graceful stop: end early but keep what was collected."""
        cam = self.cam
        for it in vec.items:
            it.value = False
        if not cam.exposing:
            self.push(vec, state=PropState.OK, message="not exposing")
            return
        # Shorten the exposure to what has actually elapsed so far.
        cam.exposure_s = max(cam.exposure_s - cam.remaining_s, 0.0)
        cam.remaining_s = 0.0
        self.push(vec, state=PropState.OK, message="exposure stopped, frame kept")
        cam.exposing = False
        self._spawn_readout()

    async def _w_frame(self, vec: Vector, values: dict[str, str]) -> None:
        s = self.sensor
        cam = self.cam
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        x = int(np.clip(vec["X"].value, 0, s.width_px - 1))
        y = int(np.clip(vec["Y"].value, 0, s.height_px - 1))
        w = int(np.clip(vec["WIDTH"].value, 1, s.width_px - x))
        h = int(np.clip(vec["HEIGHT"].value, 1, s.height_px - y))
        cam.start_x, cam.start_y, cam.num_x, cam.num_y = x, y, w, h
        vec["X"].value, vec["Y"].value = x, y
        vec["WIDTH"].value, vec["HEIGHT"].value = w, h
        self.push(vec, state=PropState.OK)

    async def _w_binning(self, vec: Vector, values: dict[str, str]) -> None:
        cam = self.cam
        for k, v in values.items():
            if k in vec:
                vec[k].value = parse_number(v)
        cam.bin_x = int(np.clip(vec["HOR_BIN"].value, 1, 4))
        cam.bin_y = int(np.clip(vec["VER_BIN"].value, 1, 4))
        vec["HOR_BIN"].value, vec["VER_BIN"].value = cam.bin_x, cam.bin_y
        self.push(vec, state=PropState.OK)

    async def _w_frame_type(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        sel = vec.selected  # type: ignore[attr-defined]
        self.cam.frame_type = FRAME_TYPES.index(sel) if sel in FRAME_TYPES else 0
        self.push(vec, state=PropState.OK)

    async def _w_gain(self, vec: Vector, values: dict[str, str]) -> None:
        vec["GAIN"].value = parse_number(values.get("GAIN", "100"))
        self.cam.gain = int(vec["GAIN"].value)
        self.push(vec, state=PropState.OK)

    async def _w_offset(self, vec: Vector, values: dict[str, str]) -> None:
        vec["OFFSET"].value = parse_number(values.get("OFFSET", "100"))
        self.cam.offset = int(vec["OFFSET"].value)
        self.push(vec, state=PropState.OK)

    async def _w_temperature(self, vec: Vector, values: dict[str, str]) -> None:
        target = parse_number(values.get("CCD_TEMPERATURE_VALUE", "0"))
        self.cam.set_temperature = target
        self.cam.cooler_on = True
        self.cooler["COOLER_ON"].value = True
        self.cooler["COOLER_OFF"].value = False
        self.push(self.cooler, state=PropState.OK)
        self.push(vec, state=PropState.BUSY)

    async def _w_cooler(self, vec: Vector, values: dict[str, str]) -> None:
        vec.apply(values)  # type: ignore[attr-defined]
        self.cam.cooler_on = vec.selected == "COOLER_ON"  # type: ignore[attr-defined]
        self.push(vec, state=PropState.OK)

    # -- exposure lifecycle ------------------------------------------------
    async def step(self, dt: float) -> None:
        cam = self.cam
        if cam.exposing and cam.remaining_s > 0:
            cam.remaining_s = max(0.0, cam.remaining_s - dt)
            self.exposure["CCD_EXPOSURE_VALUE"].value = cam.remaining_s
            self.push(self.exposure, state=PropState.BUSY)
            if cam.remaining_s <= 0:
                cam.exposing = False
                # Readout is spawned, not awaited: ``step`` is called from
                # ``IndiServer._tick``, so awaiting it would stop the simulated
                # clock for the length of a render - the mount would stop
                # tracking while the other camera reads out. A real camera does
                # not halt the sky either.
                self._spawn_readout()

        self.temperature["CCD_TEMPERATURE_VALUE"].value = cam.temperature
        power = 0.0
        if cam.cooler_on:
            drop = max(20.0 - cam.temperature, 0.0)
            power = float(np.clip(drop / 40.0 * 100.0, 0, 100))
        self.cooler_power["CCD_COOLER_VALUE"].value = power
        if int(self.rig.elapsed_s) % 2 == 0:
            self.push(self.temperature, state=PropState.OK)
            self.push(self.cooler_power)

    def _spawn_readout(self) -> None:
        """Start a readout in the background, keeping a reference to the task.

        Without the reference the task can be garbage-collected mid-flight, and
        without the callback an exception in it is never reported.
        """
        task = asyncio.ensure_future(self._finish())
        self._readout_tasks.add(task)
        task.add_done_callback(self._readout_tasks.discard)
        task.add_done_callback(
            lambda t: None if t.cancelled() or t.exception() is None
            else log.error("%s readout failed", self.device_name, exc_info=t.exception())
        )

    async def _finish(self) -> None:
        cam = self.cam
        cam.exposing = False
        cam.remaining_s = 0.0
        # Readout runs in a worker thread. Rendering a frame is expensive - a
        # survey reprojection onto a 3008x3008 sensor is seconds of numpy and
        # astropy - and this coroutine is awaited from ``IndiServer._tick``, so
        # doing it inline froze the whole server for that long: guide pulses sat
        # unread in the socket, no property updates went out, and the simulated
        # clock fell behind wall clock. numpy, scipy and astropy release the GIL,
        # so a thread keeps loop latency in the milliseconds.
        try:
            async with self.rig.capture_lock:
                frame, payload = await asyncio.to_thread(self._readout, cam)
        except Exception as exc:
            self.push(self.exposure, state=PropState.ALERT, message=f"capture failed: {exc}")
            return
        if cam.aborted:
            # Readout is no longer instantaneous, so an abort can arrive while a
            # frame is still rendering. ``_w_abort`` promised the client the
            # frame was discarded; deliver the BLOB anyway and it lands in the
            # next sequence's slot.
            return
        cam.last_frame = frame
        cam.last_exposure_s = cam.exposure_s
        cam.sequence += 1

        compress = self.compression.selected == "CCD_COMPRESS"  # type: ignore[attr-defined]
        item = self.ccd_blob.items[0]
        if compress:
            item.data = zlib.compress(payload)  # type: ignore[union-attr]
            item.fmt = ".fits.z"  # type: ignore[union-attr]
        else:
            item.data = payload  # type: ignore[union-attr]
            item.fmt = ".fits"  # type: ignore[union-attr]

        self.exposure["CCD_EXPOSURE_VALUE"].value = 0.0
        self.push(self.exposure, state=PropState.OK)
        # Only clients that sent enableBLOB Also|Only receive this.
        self.push(self.ccd_blob, state=PropState.OK)

    def _readout(self, cam: CameraState) -> tuple[np.ndarray, bytes]:
        """Render the frame and encode it. Runs off the event loop.

        Both halves belong in the thread: the FITS encode of a 3008x3008 frame
        is not free either, and ``_to_fits`` has to see the same ``cam.last_wcs``
        ``capture`` just stored.
        """
        frame = self.rig.capture(cam)
        return frame, self._to_fits(frame)

    def _to_fits(self, frame: np.ndarray) -> bytes:
        cam = self.cam
        cfg = self.rig.cfg
        s = self.sensor
        # Reuse the WCS the pixels were rendered on; rebuilding it here would
        # redraw the tracking noise and disagree with the image.
        sensor_frame = cam.last_wcs or self.rig.build_wcs(
            s.width_px, s.height_px, self.rig.scale_arcsec_px(cam)
        )
        wcs = frame_wcs(
            sensor_frame, cam.start_x, cam.start_y, cam.bin_x, cam.bin_y, frame.shape
        )

        hdu = fits.PrimaryHDU(data=frame)
        h = hdu.header
        h.update(wcs.to_header())
        h["EXPTIME"] = (cam.last_exposure_s, "[s] exposure duration")
        h["DATE-OBS"] = (cam.last_start_time, "UTC start of exposure")
        h["IMAGETYP"] = IMAGETYP[min(cam.frame_type, 3)]
        h["INSTRUME"] = self.device_name
        h["TELESCOP"] = "AstroSkySim"
        h["SWCREATE"] = "AstroSkySim"
        h["XBINNING"] = cam.bin_x
        h["YBINNING"] = cam.bin_y
        h["XPIXSZ"] = (s.pixel_size_um * cam.bin_x, "[um] binned pixel size X")
        h["YPIXSZ"] = (s.pixel_size_um * cam.bin_y, "[um] binned pixel size Y")
        # The optical train this camera actually looks through, which for the
        # guider is the guide scope when one is configured. A plate solver takes
        # FOCALLEN as its scale hint, so the main scope's value here sends it
        # hunting at the wrong field size.
        guider = self.rig.is_guider(cam)
        tel = cfg.telescope
        h["FOCALLEN"] = (
            tel.guide_focal_length if guider else tel.focal_length_mm,
            "[mm] focal length",
        )
        h["APTDIA"] = (tel.guide_aperture if guider else tel.aperture_mm, "[mm] aperture")
        h["GAIN"] = cam.gain
        h["OFFSET"] = cam.offset
        h["CCD-TEMP"] = (round(cam.temperature, 2), "[C] sensor temperature")
        h["FOCUSPOS"] = (round(self.rig.focuser.position), "focuser position")
        hfd = self.rig.guide_hfd() if guider else self.rig.current_hfd()
        h["HFD"] = (round(hfd, 3), "[px] simulated half flux diameter")
        names = cfg.filter_wheel.names
        h["FILTER"] = names[min(self.rig.filter.slot - 1, len(names) - 1)]
        h["ROTATANG"] = (round(self.rig.sky_position_angle, 3), "[deg] rotator sky angle")
        if self.rig.satellites is not None:
            # Ground truth for whatever the client does about trails: a rejection
            # stack has no other way to know whether a frame really had one.
            h["NSATS"] = (cam.last_satellites, "satellite trails simulated in this frame")
        if self.rig.wind is not None:
            # Same reasoning as NSATS: a client looking at a sub with streaked
            # stars cannot otherwise tell wind from a bad guide star or a slipped
            # clutch. SMEARPX is in unbinned sensor pixels, because the smear is
            # applied before subframing and binning.
            h["WINDKMH"] = (round(cam.last_wind_kmh, 2), "[km/h] simulated sustained wind")
            h["GUSTKMH"] = (round(cam.last_gust_kmh, 2), "[km/h] simulated wind gust")
            h["SMEARPX"] = (
                round(cam.last_smear_px, 3),
                "[px] peak-to-peak wind smear, unbinned",
            )
        # numpy row 0 is the bottom row of the FITS image.
        h["ROWORDER"] = "BOTTOM-UP"
        if s.bayer != "MONO":
            h["BAYERPAT"] = s.bayer
            h["XBAYROFF"] = 0
            h["YBAYROFF"] = 0

        buf = io.BytesIO()
        hdu.writeto(buf, overwrite=True)
        return buf.getvalue()


class Camera(CameraBase):
    device_name = "AstroSkySim CCD"
    interface = CCD_INTERFACE
    state_attr = "camera"
    blob_name = "CCD1"


class GuideCamera(GuidePulseMixin, CameraBase):
    """Guide camera with a working ST4 port.

    It claims ``GUIDER_INTERFACE``, so it must accept timed guide pulses — the
    bit is the client's advertisement that this device can be pulsed, not a
    label meaning "guide camera". ``indi_simulator_ccd`` does the same thing
    (``DRIVER_INTERFACE=22``). The pulses drive ``rig.mount``, so guiding works
    whether the client routes them here or to the telescope.
    """

    device_name = "AstroSkySim Guider"
    interface = CCD_INTERFACE | GUIDER_INTERFACE
    state_attr = "guider"
    blob_name = "CCD1"

    def setup(self) -> None:
        super().setup()
        self.add_guide_pulse_properties(group="Guider Control")

    async def step(self, dt: float) -> None:
        await super().step(dt)
        self.step_guide_pulse()
