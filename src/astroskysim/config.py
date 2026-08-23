"""Configuration model. Loaded from TOML, overridable from the CLI."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

import tomllib
from pydantic import AfterValidator, BaseModel, Field, model_validator

#: A path from the TOML, with ``~`` expanded. Without this, ``cache_dir =
#: "~/.cache/astroskysim"`` created a directory literally named ``~`` next to the
#: working directory - which looks like a working cache until you go looking
#: for it.
UserPath = Annotated[Path, AfterValidator(lambda p: p.expanduser())]


class SourceMode(str, Enum):
    """How a frame's pixels are produced."""

    #: Stars and deep-sky objects rendered from the local catalogue.
    ARTIFICIAL = "artificial"
    #: A real survey cutout, reprojected onto the sensor grid.
    DSS = "dss"
    #: Survey cutout as background, artificial stars rendered on top.
    COMPOSITE = "composite"


class Site(BaseModel):
    latitude: float = 52.37
    longitude: float = 4.90  # East positive
    elevation: float = 10.0


class Telescope(BaseModel):
    focal_length_mm: float = Field(700.0, gt=0)
    aperture_mm: float = Field(100.0, gt=0)
    #: The guide scope. Leave unset for an off-axis guider, which looks through
    #: the imaging OTA and so shares its focal length and aperture. INDI has had
    #: somewhere to report these all along (``TELESCOPE_INFO`` carries
    #: ``GUIDER_FOCAL_LENGTH``/``GUIDER_APERTURE``); before this they just echoed
    #: the main scope.
    guide_focal_length_mm: float | None = Field(None, gt=0)
    guide_aperture_mm: float | None = Field(None, gt=0)

    @property
    def separate_guide_scope(self) -> bool:
        return self.guide_focal_length_mm is not None

    @property
    def guide_focal_length(self) -> float:
        return self.guide_focal_length_mm or self.focal_length_mm

    @property
    def guide_aperture(self) -> float:
        return self.guide_aperture_mm or self.aperture_mm


class Sensor(BaseModel):
    width_px: int = Field(1200, ge=16, le=32768)
    height_px: int = Field(1000, ge=16, le=32768)
    pixel_size_um: float = Field(5.0, gt=0)
    #: 0 = mono, otherwise a Bayer pattern name.
    bayer: Literal["MONO", "RGGB", "BGGR", "GRBG", "GBRG"] = "MONO"
    bit_depth: Literal[8, 16] = 16
    well_depth_e: float = Field(20000.0, gt=0)
    read_noise_e: float = Field(3.0, ge=0)
    #: e-/ADU at gain 100 (unity-ish). Sets where the electron count lands in ADU.
    e_per_adu: float = Field(1.0, gt=0)
    hot_pixels: int = Field(0, ge=0)
    #: Dark current of a hot pixel, e-/s. Scaled by exposure time, so short
    #: guide frames are not dominated by fixed-pattern noise.
    hot_pixel_e_s: float = Field(200.0, ge=0)


class Focuser(BaseModel):
    max_position: int = Field(30000, gt=0)
    perfect_focus: int = Field(15000, ge=0)
    #: Position offset producing the configured HFD range; controls how fast
    #: the star bloats as you defocus.
    focus_range: int = Field(1000, gt=0)
    backlash: int = Field(0, ge=0)
    step_size_um: float = Field(1.0, gt=0)
    speed_steps_s: float = Field(2000.0, gt=0)
    temperature: float = 12.0
    #: steps per degree C, applied when temperature compensation is enabled.
    temp_coeff: float = 0.0


class Rotator(BaseModel):
    speed_deg_s: float = Field(4.0, gt=0)
    #: Offset between mechanical and sky position angle, in degrees.
    mechanical_offset: float = 0.0
    reversed: bool = False


class FilterWheel(BaseModel):
    names: list[str] = Field(default_factory=lambda: ["L", "R", "G", "B", "Ha"])
    #: Per-filter focus offsets in steps, so a per-filter autofocus run has
    #: something real to find.
    focus_offsets: list[int] = Field(default_factory=lambda: [0, 0, 0, 0, 120])
    #: Per-filter transmission as a fraction of the unfiltered throughput.
    #: Multiplies the *whole* light path - stars, nebulosity and sky alike, as
    #: a real filter does - so a narrowband sub is genuinely starved and needs
    #: the exposure to match. Unset means 1.0 everywhere, which is what every
    #: config before this got. Sensible broadband values are 1.0 for L and
    #: ~0.3 for each of R/G/B; a 3 nm Ha passes ~0.01 of a luminance band.
    transmission: list[float] | None = None
    change_time_s: float = Field(1.5, ge=0)

    @model_validator(mode="after")
    def _same_length(self) -> FilterWheel:
        if len(self.focus_offsets) != len(self.names):
            raise ValueError(
                f"focus_offsets has {len(self.focus_offsets)} entries "
                f"but there are {len(self.names)} filters"
            )
        if self.transmission is not None and len(self.transmission) != len(self.names):
            raise ValueError(
                f"transmission has {len(self.transmission)} entries "
                f"but there are {len(self.names)} filters"
            )
        return self

    def transmission_for(self, slot: int) -> float:
        """Transmission of a 1-based filter slot, clamped to the wheel."""
        if not self.transmission:
            return 1.0
        idx = max(0, min(slot - 1, len(self.transmission) - 1))
        return max(self.transmission[idx], 0.0)


class MountConfig(BaseModel):
    equatorial: bool = True
    slew_rate_deg_s: float = Field(3.0, gt=0)
    #: Guide rate as a fraction of sidereal.
    guide_rate: float = Field(0.5, gt=0, le=1.0)
    #: Polar alignment error, arcminutes.
    azimuth_error: float = 0.0
    elevation_error: float = 0.0
    #: Periodic error amplitude (arcsec) and period (s).
    periodic_error_amplitude: float = 0.0
    periodic_error_period: float = 480.0
    #: Random tracking noise, arcsec RMS.
    tracking_noise: float = 0.0
    park_ra_hours: float = 0.0
    park_dec_deg: float = 90.0


class Optics(BaseModel):
    seeing_arcsec: float = Field(2.5, gt=0)
    #: Sky brightness in mag/arcsec^2 - an SQM reading. Converted to e-/px/s
    #: through the aperture, plate scale and throughput below, so a change of
    #: telescope or of camera moves the background with it. 21.0 is a decent
    #: rural site, 20.0 suburban, 18.0 a city centre.
    sky_mag_arcsec2: float = Field(21.0, gt=0)
    #: Sky background straight in e-/px/s, overriding ``sky_mag_arcsec2``.
    #: This is the field that used to be called *the* sky background, and its
    #: unit is the trap: ``sky_background = 21.0`` looks like an SQM reading
    #: and is in fact roughly SQM 18 on a small refractor. Prefer
    #: ``sky_mag_arcsec2``; ``build_rig`` warns when this override is in use.
    sky_background: float | None = Field(None, ge=0)
    #: End-to-end optical + quantum efficiency, before any filter. One number
    #: standing in for coatings, obstruction losses and QE.
    throughput: float = Field(0.5, gt=0, le=1.0)
    #: e-/s/m^2 from a magnitude 0 source, integrated over the band. The
    #: anchor for stars, sky and survey cutouts alike - move it and the whole
    #: photometric scale moves together.
    zero_point_e_s_m2: float = Field(1.0e10, gt=0)
    #: Fixed HFD (px) for the guide camera. A separate guide scope is focused
    #: once and left alone, so the main focuser must not blur the guide star -
    #: otherwise an autofocus run bloats it to 10 px and guiding is lost
    #: mid-sequence, which no real rig does. Unset, the guider follows the
    #: focuser, which is right for an off-axis guider.
    guide_hfd_px: float | None = Field(None, gt=0)


class ArtificialSource(BaseModel):
    #: Directory holding the HNSKY ``.290`` star database files.
    catalog_dir: UserPath | None = None
    #: Preferred database prefix, e.g. "g14". Falls back through known names.
    catalog: str = "g14"
    limiting_mag: float = 16.0
    #: Used when no catalogue files are present, so the simulator still runs.
    allow_synthetic_fallback: bool = True


class SurveyLayer(BaseModel):
    """One survey and the photometric anchor that turns it into electrons.

    A ``[source.dss.per_filter.<name>]`` section is one of these, attached to
    the filter of that name. The anchor is what makes the layers comparable:
    ``ref_mag_arcsec2`` is the surface brightness that the reference level of
    the background-subtracted cutout stands for, and everything downstream -
    aperture, plate scale, throughput, exposure - follows from the optics.

    The reference level itself comes from one of two places:

    * ``ref_value`` - a raw survey pixel value above the survey's own sky. Use
      this whenever the survey is linear and internally calibrated, because it
      preserves *both* the ratios between its bands and the difference between
      a bright target and a faint one. The three NSNS line maps share one
      scale, so giving Ha, OIII and SII the same ``ref_value`` reproduces the
      real line ratios: an HII region comes out Ha-dominated and a planetary
      nebula OIII-dominated, without either being configured that way.
    * ``ref_percentile`` - the fallback when the units are unknown or the
      response is non-linear, as on the photographic DSS plates. It normalises
      each cutout against itself, which is robust but flattens the sky: an
      empty field then renders as brightly as M42.

    Reprojection preserves surface brightness rather than flux, so a
    ``ref_value`` calibrated once holds for any sensor and plate scale.
    """

    #: ``hips:<HiPS id>``, ``skyview:<Survey>`` or ``eso:<Sky-Survey>``.
    survey: str = "hips:CDS/P/DSS2/red"
    ref_mag_arcsec2: float = Field(19.5, gt=0)
    ref_percentile: float = Field(99.0, gt=50.0, lt=100.0)
    #: Absolute anchor in the survey's own units, above its own sky. Overrides
    #: ``ref_percentile`` when set.
    ref_value: float | None = Field(None, gt=0)
    #: True when this survey *is* the object as the filter sees it, which is
    #: the point of attaching it to that filter. The filter's ``transmission``
    #: is a broadband fraction: right for a star or the sky, whose light the
    #: filter throws away, and wrong for an image already taken in that band -
    #: a 3 nm Ha filter does not dim an Ha map by fifty. So an in-band layer is
    #: exempt from it, while stars and sky are not. Set false to model imaging
    #: a broadband proxy through a narrow filter, which *is* attenuated.
    in_band: bool = True


class DssSource(BaseModel):
    #: ``hips:<HiPS id>``, ``skyview:<Survey>`` or ``eso:<Sky-Survey>``.
    #: hips2fits is the default: it reaches every HiPS on the CDS list, including
    #: the narrowband ones no other back end here can serve.
    #:
    #: This is the *default* layer: the guide camera always uses it (its pickoff
    #: prism is upstream of the filter wheel), and so does any filter without a
    #: ``per_filter`` entry.
    survey: str = "hips:CDS/P/DSS2/red"
    cache_dir: UserPath | None = None
    timeout_s: float = Field(60.0, gt=0)
    #: Fall back to the artificial sky if the fetch fails, rather than erroring.
    fallback_to_artificial: bool = True
    #: Refuse a cutout covering less than this fraction of the sensor. Partial-sky
    #: surveys, and surveys that mask their saturated cores, otherwise deliver a
    #: frame that is mostly black hole where the target should be.
    min_coverage: float = Field(0.5, ge=0.0, le=1.0)
    #: Ceiling on the pixel grid requested from hips2fits, which resamples to
    #: whatever it is asked for.
    max_download_px: int = Field(3000, ge=300, le=10000)
    #: Photometric anchor for the cutout: the surface brightness, in
    #: mag/arcsec^2, that the reference level of the background-subtracted
    #: survey pixels stands for. Everything else - aperture, plate scale,
    #: throughput, filter, exposure - follows from the optics. Raise the
    #: magnitude to dim the nebulosity. See ``SurveyLayer``.
    ref_mag_arcsec2: float = Field(19.5, gt=0)
    ref_percentile: float = Field(99.0, gt=50.0, lt=100.0)
    ref_value: float | None = Field(None, gt=0)
    #: The default layer is a stand-in for whatever filter is in the beam, not
    #: a match for it, so it *is* attenuated by that filter's transmission.
    #: Per-filter layers default the other way. See ``SurveyLayer.in_band``.
    in_band: bool = False
    #: Survey per filter, keyed by the name in ``[filter_wheel] names``.
    #: Unlisted filters fall back to the default layer above.
    per_filter: dict[str, SurveyLayer] = Field(default_factory=dict)

    @property
    def default_layer(self) -> SurveyLayer:
        return SurveyLayer(
            survey=self.survey,
            ref_mag_arcsec2=self.ref_mag_arcsec2,
            ref_percentile=self.ref_percentile,
            ref_value=self.ref_value,
            in_band=self.in_band,
        )


class CompositeSource(BaseModel):
    #: Weight of the survey background when blended under artificial stars.
    background_weight: float = Field(1.0, ge=0)
    #: Weight of the rendered artificial stars.
    star_weight: float = Field(1.0, ge=0)
    #: Subtract an estimated stellar component from the survey background so
    #: real stars do not double up with the rendered ones.
    suppress_background_stars: bool = True


class SourceConfig(BaseModel):
    mode: SourceMode = SourceMode.ARTIFICIAL
    artificial: ArtificialSource = Field(default_factory=ArtificialSource)
    dss: DssSource = Field(default_factory=DssSource)
    composite: CompositeSource = Field(default_factory=CompositeSource)


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(7624, ge=1, le=65535)
    tick_hz: float = Field(10.0, gt=0, le=100)
    device_prefix: str = "AstroSkySim"
    #: Which devices to advertise.
    mount: bool = True
    camera: bool = True
    guide_camera: bool = True
    focuser: bool = True
    rotator: bool = True
    filter_wheel: bool = True


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    site: Site = Field(default_factory=Site)
    telescope: Telescope = Field(default_factory=Telescope)
    sensor: Sensor = Field(default_factory=Sensor)
    #: The guide camera's sensor. Real guide cameras are small, uncooled and
    #: coarse-pixelled next to the imaging chip, so sharing one spec makes the
    #: two cameras indistinguishable to a client. Left unset the guider falls
    #: back to ``sensor``, which is what every config before this did.
    sensor_guide_cam: Sensor | None = None
    focuser: Focuser = Field(default_factory=Focuser)
    rotator: Rotator = Field(default_factory=Rotator)
    filter_wheel: FilterWheel = Field(default_factory=FilterWheel)
    mount: MountConfig = Field(default_factory=MountConfig)
    optics: Optics = Field(default_factory=Optics)
    source: SourceConfig = Field(default_factory=SourceConfig)
    #: Fixed RNG seed makes a run reproducible; None seeds from the OS.
    seed: int | None = 1234

    @model_validator(mode="after")
    def _per_filter_names_exist(self) -> Config:
        """A ``per_filter`` key must name a real filter.

        Silently ignoring a typo is the worst outcome: the filter quietly keeps
        the default broadband survey and the frames look almost right, so the
        mistake surfaces as "my Ha subs are too bright" weeks later.
        """
        unknown = [n for n in self.source.dss.per_filter if n not in self.filter_wheel.names]
        if unknown:
            raise ValueError(
                f"source.dss.per_filter names no such filter: {', '.join(unknown)}; "
                f"filter_wheel.names are {', '.join(self.filter_wheel.names)}"
            )
        return self

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None:
            return cls()
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    # -- derived -----------------------------------------------------------
    @property
    def guide_sensor(self) -> Sensor:
        """The guide camera's sensor, or the imaging one if none is configured."""
        return self.sensor_guide_cam or self.sensor

    @staticmethod
    def _scale(pixel_size_um: float, focal_length_mm: float) -> float:
        return 206.264806 * pixel_size_um / focal_length_mm

    @property
    def scale_arcsec_px(self) -> float:
        """Plate scale of one unbinned imaging pixel."""
        return self._scale(self.sensor.pixel_size_um, self.telescope.focal_length_mm)

    @property
    def guide_scale_arcsec_px(self) -> float:
        """Plate scale of one unbinned guide pixel, through the guide scope."""
        return self._scale(self.guide_sensor.pixel_size_um, self.telescope.guide_focal_length)

    @property
    def fov_deg(self) -> tuple[float, float]:
        s = self.scale_arcsec_px / 3600.0
        return self.sensor.width_px * s, self.sensor.height_px * s

    @property
    def guide_fov_deg(self) -> tuple[float, float]:
        s = self.guide_scale_arcsec_px / 3600.0
        return self.guide_sensor.width_px * s, self.guide_sensor.height_px * s
