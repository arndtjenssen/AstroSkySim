"""Image source interface.

Every source delivers electrons on **the sensor's own pixel grid**. That is the
whole contract. Sources reproject to fit; the sensor never moves. Letting a
survey's plate scale set the frame size instead would resize CCD_INFO under a
connected client, which no client expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
from astropy.wcs import WCS

from ..sky.render import Optics


@dataclass(slots=True)
class RenderContext:
    """Everything a source needs to produce one frame."""

    wcs: WCS
    shape: tuple[int, int]
    optics: Optics
    exposure_s: float
    rng: np.random.Generator
    #: Sky background in electrons/pixel/second.
    sky_e_s: float = 20.0
    #: Name of the filter in the beam, for sources that serve a different image
    #: per filter. ``None`` means "no filter applies" - which is the guide
    #: camera, whose pickoff prism sits upstream of the wheel.
    filter_name: str | None = None
    #: That filter's broadband transmission, **already folded into
    #: ``optics.throughput``**. Carried separately only so an in-band source can
    #: take it back out: a survey shot through the Ha filter is not dimmed by
    #: the Ha filter, while the stars and sky around it are.
    filter_transmission: float = 1.0

    @property
    def center(self) -> tuple[float, float]:
        return float(self.wcs.wcs.crval[0]), float(self.wcs.wcs.crval[1])

    @property
    def radius_deg(self) -> float:
        """Radius of the circle circumscribing the frame."""
        h, w = self.shape
        s = self.optics.scale_arcsec_px / 3600.0
        return 0.5 * float(np.hypot(w * s, h * s))


@runtime_checkable
class ImageSource(Protocol):
    """Produces a frame in electrons, shaped exactly ``ctx.shape``."""

    name: str

    def render(self, ctx: RenderContext) -> np.ndarray: ...


class SourceError(RuntimeError):
    """A source could not produce a frame."""


def calibrate_survey_image(
    data: np.ndarray,
    ref_percentile: float = 99.0,
    ref_value: float | None = None,
) -> np.ndarray:
    """Survey units to *signal above the survey's own sky*, in units of the
    reference level.

    The caller multiplies the result by the electron rate of one reference
    surface brightness, so 1.0 here means "as bright as ``ref_mag_arcsec2``".

    ``ref_value`` sets that level absolutely, in the survey's own units. It is
    the better choice for any linear, internally calibrated survey, because the
    percentile below normalises every cutout against itself and so throws away
    two real things: the brightness ratio between a survey's bands, and the
    difference between a bright target and an empty field. With a shared
    ``ref_value`` the NSNS line maps keep both - M42 outruns a blank field by
    four orders of magnitude, and M27 comes out OIII-dominated while IC 1805
    comes out Ha-dominated, neither of them configured that way.

    The percentile remains the default because it needs to know nothing about
    the survey, which is what the photographic DSS plates require: their
    density scale is non-linear and each plate carries its own pedestal.

    This replaces a 5-99.5 percentile stretch to [0, 1], which had three
    defects that together made exposure time meaningless:

    * it mapped the field's own 5th percentile to **zero flux**, i.e. 5% of
      every frame came out darker than the sky, which no detector can produce;
    * the survey's sky level landed at an arbitrary height above that zero, so
      the object-to-sky contrast came from the plate's density curve rather
      than from photometry;
    * the caller then applied a fixed electrons-per-second, so a 1 s sub of a
      faint nebula arrived at the same brightness as a 20 s one of a bright
      one.

    Here the survey's sky is *estimated and subtracted* instead, leaving only
    what the object adds; the sky is put back downstream from the configured
    SQM. Blanks (NaN, from a footprint edge or a masked core) go to zero, which
    now correctly means "sky only" rather than "black hole".

    Estimating the sky as the median of the lower half is deliberate: a plain
    median sits well above sky in a frame the nebula fills, and a low
    percentile sits in the noise below it.
    """
    finite = np.isfinite(data)
    if not finite.any():
        return np.zeros_like(data, dtype=np.float64)
    values = np.asarray(data, dtype=np.float64)[finite]
    lower = values[values <= np.median(values)]
    background = float(np.median(lower)) if lower.size else float(np.median(values))

    above = np.clip(np.nan_to_num(np.asarray(data, dtype=np.float64), nan=background) - background, 0.0, None)
    ref = (
        float(ref_value)
        if ref_value is not None
        else float(np.percentile(above[finite], ref_percentile))
    )
    if not np.isfinite(ref) or ref <= 0.0:
        # A flat frame carries no object signal; the sky is added downstream.
        return np.zeros_like(data, dtype=np.float64)
    # Not clipped at 1.0: the survey's own bright cores belong above the
    # reference level, and the well depth is what limits them.
    return above / ref
