"""One survey per filter.

A single survey behind a filter wheel gives every filter the same picture, only
dimmer - which is exactly wrong for narrowband, where the whole point is that
Ha, OIII and SII show *different structure*, not the same structure at different
brightness. This dispatches on the filter in the beam so each one gets the
survey that actually matches its band.

Two rules make the result mean something, and both live outside this file:

* ``SurveyLayer.in_band`` (``config.py``) exempts a matched survey from the
  filter's broadband transmission - a 3 nm Ha filter does not dim an Ha map.
* ``SurveyLayer.ref_value`` (``sources/base.py``) anchors a linear survey
  absolutely rather than per frame, which is what lets one number cover Ha,
  OIII and SII and still produce their real ratio.

The guide camera never dispatches: ``ctx.filter_name`` is ``None`` for it,
because its pickoff prism sits upstream of the wheel - the same reason
``Rig.guide_hfd`` drops the per-filter focus offset.
"""

from __future__ import annotations

import logging

import numpy as np

from .base import ImageSource, RenderContext

log = logging.getLogger("astroskysim.sources.filtered")


class FilterSurveySource:
    """Pick the survey attached to ``ctx.filter_name``, else the default one."""

    def __init__(self, default: ImageSource, per_filter: dict[str, ImageSource]) -> None:
        self.default = default
        self.per_filter = dict(per_filter)
        self.name = f"{default.name}[{'|'.join(sorted(self.per_filter))}]"

    def render(self, ctx: RenderContext) -> np.ndarray:
        source = self.default
        if ctx.filter_name is not None:
            source = self.per_filter.get(ctx.filter_name, self.default)
        return source.render(ctx)
