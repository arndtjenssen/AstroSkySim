"""Satellite light pollution: orbital elements in, streaks on the frame out.

Three modules, split along the same seam as the star catalogue:

* ``config.py`` — the **shared** source list, which deliberately lives outside
  any rig config. Which satellites are in the sky is a property of the machine
  and the date, not of a telescope, so every ``sim.toml`` points at one file.
* ``tle.py`` — the Celestrak source registry, the download, and the parse.
* ``trails.py`` — SGP4 propagation, the illumination model, and the streak.

Copyright (C) 2025 AstroSkySim contributors. GPLv3-or-later; see LICENSE.
"""

from __future__ import annotations

from .config import (
    DEFAULT_CONFIG_PATH,
    SatellitesConfig,
    SatelliteSource,
    default_config_text,
    discover_config,
    load_satellites_config,
)
from .tle import fetch_sources, parse_tle_text, source_url
from .trails import SatelliteSky, apparent_magnitude, build_satellite_sky

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "SatelliteSky",
    "SatelliteSource",
    "SatellitesConfig",
    "apparent_magnitude",
    "build_satellite_sky",
    "default_config_text",
    "discover_config",
    "fetch_sources",
    "load_satellites_config",
    "parse_tle_text",
    "source_url",
]
