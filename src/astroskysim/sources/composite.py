"""Composite source: artificial stars rendered over a real survey background.

Compositing gives you real nebulosity *and* stars at catalogue-known
positions, which is what makes the frame useful as ground truth for plate
solving and photometry checks. Either source alone gives you one or the other.

By default the survey's own point sources are suppressed first, so a real star
and its rendered counterpart do not stack into a double-brightness artefact.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import RenderContext

log = logging.getLogger("astroskysim.sources.composite")

#: Typical DSS plate seeing. Sets how wide a feature counts as "a star" when
#: suppressing the survey's own point sources.
SURVEY_SEEING_ARCSEC = 3.0


def suppress_point_sources(
    image: np.ndarray, scale_arcsec_px: float, seeing_arcsec: float = SURVEY_SEEING_ARCSEC
) -> np.ndarray:
    """Remove star-sized features, keep extended structure.

    Grey opening (erode then dilate) with a footprint a bit wider than the
    survey PSF deletes compact peaks while leaving nebulosity and galaxies
    largely intact.
    """
    from scipy import ndimage

    radius_px = max(seeing_arcsec / max(scale_arcsec_px, 1e-6), 1.0)
    size = int(np.ceil(radius_px * 2.5))
    size = max(3, min(size | 1, 51))  # odd, and bounded so it stays cheap
    return ndimage.grey_opening(image, size=(size, size))


class CompositeSource:
    """Survey background plus rendered artificial stars."""

    name = "composite"

    def __init__(
        self,
        background,
        stars,
        *,
        background_weight: float = 1.0,
        star_weight: float = 1.0,
        suppress_background_stars: bool = True,
    ) -> None:
        self.background = background
        self.stars = stars
        self.background_weight = background_weight
        self.star_weight = star_weight
        self.suppress_background_stars = suppress_background_stars

    def render(self, ctx: RenderContext) -> np.ndarray:
        from .base import SourceError

        try:
            bg = self.background.render(ctx)
        except SourceError as exc:
            # A composite frame without its background is still a usable frame.
            log.warning("composite background failed (%s), stars only", exc)
            bg = np.zeros(ctx.shape, dtype=np.float64)

        if self.suppress_background_stars and bg.any():
            bg = suppress_point_sources(bg, ctx.optics.scale_arcsec_px)

        stars = self.stars.render(ctx)
        return self.background_weight * bg + self.star_weight * stars
