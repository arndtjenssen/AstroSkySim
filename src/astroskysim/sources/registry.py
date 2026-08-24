"""Build the configured image source."""

from __future__ import annotations

import logging

from ..config import Config, SourceMode, SurveyLayer
from ..sky.catalog import build_catalog
from .artificial import ArtificialSource
from .base import ImageSource
from .composite import CompositeSource
from .dss import DssSource, FallbackSource, HipsEndpoint
from .filtered import FilterSurveySource

log = logging.getLogger("astroskysim.sources")


def _build_layer(layer: SurveyLayer, cfg: Config, hips: HipsEndpoint) -> DssSource:
    """One survey, with the transport settings shared by every layer."""
    dss = cfg.source.dss
    return DssSource(
        survey=layer.survey,
        cache_dir=dss.cache_dir,
        timeout_s=dss.timeout_s,
        hips=hips,
        min_coverage=dss.min_coverage,
        max_download_px=dss.max_download_px,
        ref_mag_arcsec2=layer.ref_mag_arcsec2,
        ref_percentile=layer.ref_percentile,
        ref_value=layer.ref_value,
        in_band=layer.in_band,
    )


def _build_survey(cfg: Config) -> ImageSource:
    """The default survey, or a per-filter dispatcher over several."""
    # One endpoint for every layer: which CDS host is alive is a property of the
    # network, not of a filter, so discovering it once per process is the point.
    hips = HipsEndpoint(
        cfg.source.dss.hips_bases, cfg.source.dss.hips_probe_timeout_s
    )
    default = _build_layer(cfg.source.dss.default_layer, cfg, hips)
    per_filter = cfg.source.dss.per_filter
    if not per_filter:
        return default
    for name, layer in per_filter.items():
        anchor = (
            f"ref_value {layer.ref_value:g}"
            if layer.ref_value is not None
            else f"p{layer.ref_percentile:g}"
        )
        log.info(
            "filter %s -> %s (%s = %.1f mag/arcsec2%s)",
            name,
            layer.survey,
            anchor,
            layer.ref_mag_arcsec2,
            "" if layer.in_band else ", attenuated by the filter",
        )
    return FilterSurveySource(
        default, {n: _build_layer(layer, cfg, hips) for n, layer in per_filter.items()}
    )


def build_source(cfg: Config) -> ImageSource:
    """Map ``config.source.mode`` onto a concrete source."""
    art_cfg = cfg.source.artificial
    catalog = build_catalog(
        art_cfg.catalog_dir,
        art_cfg.catalog,
        allow_synthetic=art_cfg.allow_synthetic_fallback,
        seed=cfg.seed,
        limiting_mag=art_cfg.limiting_mag,
    )
    artificial = ArtificialSource(catalog, art_cfg.limiting_mag)

    if cfg.source.mode is SourceMode.ARTIFICIAL:
        return artificial

    dss = _build_survey(cfg)

    if cfg.source.mode is SourceMode.DSS:
        if cfg.source.dss.fallback_to_artificial:
            return FallbackSource(dss, artificial)
        return dss

    if cfg.source.mode is SourceMode.COMPOSITE:
        return CompositeSource(
            background=dss,
            stars=artificial,
            background_weight=cfg.source.composite.background_weight,
            star_weight=cfg.source.composite.star_weight,
            suppress_background_stars=cfg.source.composite.suppress_background_stars,
        )

    raise ValueError(f"unhandled source mode {cfg.source.mode!r}")
