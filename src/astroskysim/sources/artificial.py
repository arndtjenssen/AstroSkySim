"""Artificial sky: stars rendered from the local catalogue."""

from __future__ import annotations

import logging

import numpy as np

from ..sky.catalog import StarField
from ..sky.render import render_stars
from .base import RenderContext

log = logging.getLogger("astroskysim.sources.artificial")


class ArtificialSource:
    """Renders the catalogue onto the sensor grid."""

    name = "artificial"

    def __init__(self, catalog, limiting_mag: float = 16.0) -> None:
        self.catalog = catalog
        self.limiting_mag = limiting_mag

    def field(self, ctx: RenderContext) -> StarField:
        ra, dec = ctx.center
        # Widen a little so stars just outside the frame still bleed in.
        field = self.catalog.query(ra, dec, ctx.radius_deg * 1.15)
        return field.brighter_than(self.limiting_mag)

    def render(self, ctx: RenderContext) -> np.ndarray:
        field = self.field(ctx)
        if len(field) == 0:
            log.debug("no catalogue stars at %.4f %+.4f", *ctx.center)
            return np.zeros(ctx.shape, dtype=np.float64)
        return render_stars(
            field.ra, field.dec, field.mag, ctx.wcs, ctx.shape, ctx.optics, ctx.exposure_s
        )
