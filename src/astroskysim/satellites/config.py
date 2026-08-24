"""The satellite configuration, which is shared rather than per-rig.

Everything here answers "what is in orbit and how bright is it", which is a
property of the machine and of the week — not of a telescope. So it lives in
its own file, found by search rather than named by each rig config, and one
``fetch-satellites`` download serves every ``sim.toml`` on the box. A rig config
carries only a pointer and an off switch (``[satellites]`` in ``config.py``).

The source list mirrors the Stellarium satellites plugin: a menu of Celestrak
groups, each with a tick box. Ticked ones are downloaded and put in the sky;
unticked ones stay in the file as documentation of what else is available. The
defaults are the four Stellarium ships ticked (``visual``, ``stations``,
``starlink``, ``science``) plus ``oneweb``, because a 2020s sky without the two
big LEO constellations is not the sky anyone is imaging under.

The written template is *generated from* ``DEFAULT_SOURCES`` rather than
maintained beside it, so the file a user edits and the defaults a user gets
without a file cannot drift apart. ``tests/test_satellites.py`` parses the
template back and compares.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import tomllib
from pydantic import BaseModel, Field, model_validator

from ..config import SatellitesRef, UserPath

log = logging.getLogger("astroskysim.satellites.config")

#: Where a config lands when nothing else is found, and what ``fetch-satellites``
#: writes when it has to create one. ``XDG_CONFIG_HOME`` wins where it is set.
DEFAULT_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "astroskysim"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "satellites.toml"

#: Searched in order. The first that exists wins; if none does, the built-in
#: defaults are used, so the feature behaves identically with and without a file.
SEARCH_PATH: tuple[Path, ...] = (Path("satellites.toml"), DEFAULT_CONFIG_PATH)


class SatelliteSource(BaseModel):
    """One TLE list: a Celestrak group, or any URL serving two-line elements."""

    #: Celestrak GP group, e.g. ``starlink``. The URL is derived from it, so a
    #: group entry survives Celestrak changing its query string.
    group: str | None = None
    #: Full URL, for a list Celestrak does not serve under a group name.
    #: Requires ``name``, which is also the cache file's stem.
    url: str | None = None
    name: str | None = None
    #: Ticked in the Stellarium sense: this list is fetched and put in the sky.
    #: An unticked entry is a menu item, not a download.
    enabled: bool = False
    #: Standard magnitude for every object in this list: the visual magnitude
    #: the satellite would have at 1000 km range and 90 deg phase angle. One
    #: number per list is a coarse model — real per-object standard magnitudes
    #: need a magnitude database (Mike McCants' ``qs.mag``), which no Celestrak
    #: TLE carries — but it does put an ISS pass and a Starlink pass four
    #: hundred times apart in brightness, which is the ordering that matters.
    #: Unset falls back to ``default_std_mag``.
    std_mag: float | None = None
    #: Written into the generated template as a comment above the entry.
    note: str = ""

    @model_validator(mode="after")
    def _one_of_group_or_url(self) -> SatelliteSource:
        if bool(self.group) == bool(self.url):
            raise ValueError("a satellite source needs exactly one of `group` or `url`")
        if self.url and not self.name:
            raise ValueError(f"the url source {self.url!r} needs a `name` (used as its file name)")
        if self.name is None:
            self.name = self.group
        return self

    @property
    def key(self) -> str:
        """Stem of this source's cached TLE file."""
        return self.name or self.group or "unnamed"


#: The menu, with the defaults ticked. Magnitudes are **estimates** from
#: published observing campaigns and the usual satellite-observer tables, not
#: measurements made here; treat them as an ordering, not photometry.
DEFAULT_SOURCES: tuple[SatelliteSource, ...] = (
    SatelliteSource(
        group="visual",
        enabled=True,
        std_mag=2.5,
        note="The ~170 brightest objects - the classic naked-eye list.",
    ),
    SatelliteSource(
        group="stations",
        enabled=True,
        std_mag=-0.5,
        note="ISS and CSS. Brighter than anything else up there; a pass through "
        "a sub is not subtle.",
    ),
    SatelliteSource(
        group="starlink",
        enabled=True,
        std_mag=5.9,
        note="~8000 objects, and the reason this feature exists. Sun visors and "
        "dielectric mirrors put the v1.5/v2 mini standard magnitude near 6.",
    ),
    SatelliteSource(
        group="oneweb",
        enabled=True,
        std_mag=7.0,
        note="~650 objects at 1200 km: slower across the frame than Starlink, "
        "and fainter for the range.",
    ),
    SatelliteSource(
        group="science",
        enabled=True,
        std_mag=5.0,
        note="Hubble, Swift and company. Few, and mostly faint.",
    ),
    SatelliteSource(
        group="active",
        std_mag=6.0,
        note="Everything operational, ~12000 objects. A superset of most lists "
        "below - tick this *instead of* them, not as well.",
    ),
    SatelliteSource(
        group="last-30-days",
        std_mag=6.0,
        note="Recent launches, often still flying in a train.",
    ),
    SatelliteSource(group="qianfan", enabled=True, std_mag=6.5, note="Chinese LEO broadband, growing fast."),
    SatelliteSource(group="kuiper", enabled=True, std_mag=6.5, note="Amazon's constellation."),
    SatelliteSource(
        group="iridium-NEXT",
        std_mag=6.5,
        note="The flaring Block 1 satellites are all deorbited; these do not flare.",
    ),
    SatelliteSource(group="globalstar", std_mag=6.0),
    SatelliteSource(group="orbcomm", std_mag=6.5),
    SatelliteSource(group="swarm", std_mag=8.0),
    SatelliteSource(group="amateur", std_mag=6.5),
    SatelliteSource(group="cubesat", std_mag=8.0),
    SatelliteSource(group="weather", std_mag=5.0),
    SatelliteSource(group="noaa", std_mag=5.0),
    SatelliteSource(group="resource", std_mag=6.0),
    SatelliteSource(group="planet", std_mag=7.5),
    SatelliteSource(group="spire", std_mag=8.0),
    SatelliteSource(
        group="geo",
        std_mag=8.0,
        note="Geostationary. These do not race across the frame - they sit still "
        "over the ground and so drift at the sidereal rate against the stars, "
        "drawing a short trail in a long sub rather than a long one.",
    ),
    SatelliteSource(group="gnss", enabled=True, std_mag=6.5, note="All navigation constellations."),
    SatelliteSource(group="gps-ops", enabled=True, std_mag=6.5),
    SatelliteSource(group="galileo", enabled=True, std_mag=6.5),
    SatelliteSource(group="military", std_mag=6.0),
    SatelliteSource(group="radar", std_mag=6.0),
    SatelliteSource(
        group="cosmos-2251-debris",
        std_mag=9.0,
        note="Debris: thousands of objects, all faint. A fair stress test of the "
        "propagation budget.",
    ),
    SatelliteSource(group="fengyun-1c-debris", std_mag=9.0),
    SatelliteSource(
        name="amsat",
        url="https://www.amsat.org/amsat/ftp/keps/current/nasabare.txt",
        std_mag=7.0,
        note="Any URL serving two-line elements works, not just Celestrak. A "
        "`url` entry needs a `name`, which is what its cache file is called.",
    ),
)


class SatellitesConfig(BaseModel):
    """The shared satellite configuration file, or its built-in defaults."""

    #: Master switch. A rig config can force this off (never on) - see
    #: ``config.SatellitesRef``.
    enabled: bool = True
    #: Where ``fetch-satellites`` writes, and where a run reads. One directory
    #: per machine is the point of this file living outside the rig config.
    tle_dir: UserPath = Path("~/.cache/astroskysim/tle").expanduser()
    #: Warn when the newest element set is older than this. SGP4 error grows
    #: roughly a kilometre a day for LEO, which is arcminutes of pointing, so
    #: stale elements put the streak in the wrong part of the frame - or in the
    #: wrong frame. Not a refusal: a plausible streak beats no streak.
    max_age_days: float = Field(14.0, gt=0)
    #: ``fetch-satellites`` skips a file younger than this unless ``--force``.
    #: Celestrak asks clients not to re-download a group more than a few times a
    #: day, and elements are only reissued that often anyway.
    refetch_after_hours: float = Field(12.0, ge=0)
    #: Standard magnitude for a source that does not set its own.
    default_std_mag: float = 6.0
    #: Draw only sunlit satellites. Off, everything in the field streaks, which
    #: is wrong in the middle of the night and useful for a deterministic test.
    require_sunlit: bool = True
    #: Ceiling on how many objects are propagated. Exceeded, the excess is
    #: dropped **with a warning naming the count** - a silent cap here reads as
    #: "your sky is quiet tonight".
    max_satellites: int = Field(30000, gt=0)
    #: Time step of the search pass that decides which satellites come near the
    #: field. Every satellite is propagated at every step, so this is the cost
    #: knob: a 300 s sub at 5 s over 12000 objects is ~730k propagations, a
    #: few tenths of a second in the readout thread. The search cone grows with
    #: it (a LEO satellite covers up to ~2.5 deg/s), so a larger step is not
    #: obviously cheaper.
    coarse_step_s: float = Field(5.0, gt=0, le=60.0)
    timeout_s: float = Field(60.0, gt=0)
    sources: list[SatelliteSource] = Field(default_factory=lambda: list(DEFAULT_SOURCES))

    @property
    def active_sources(self) -> list[SatelliteSource]:
        return [s for s in self.sources if s.enabled]

    def std_mag_for(self, source: SatelliteSource) -> float:
        return self.default_std_mag if source.std_mag is None else source.std_mag

    @classmethod
    def load(cls, path: str | Path) -> SatellitesConfig:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)


def _toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _display_path(path: Path) -> str:
    """``~``-relative where it can be. The generated file is checked into the
    repository as ``examples/satellites.toml``, and one developer's home
    directory has no business being in it."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def default_config_text() -> str:
    """The shipped ``satellites.toml``, generated from the defaults above.

    Generated rather than hand-written so the file a user edits and the defaults
    a user gets with no file are the same thing by construction.
    """
    cfg = SatellitesConfig()
    lines = [
        "# AstroSkySim satellite configuration.",
        "#",
        "# Shared on purpose: this file is found by search, not named by each rig",
        "# config, so one element download and one source list serve every sim.toml",
        "# on the machine. Searched in order:",
        "#",
        "#   1. --satellites FILE",
        "#   2. [satellites] config = \"...\" in the rig config",
        "#   3. ./satellites.toml",
        f"#   4. {_display_path(DEFAULT_CONFIG_PATH)}",
        "#",
        "# Populate it with:  astroskysim fetch-satellites",
        "",
        f"enabled = {str(cfg.enabled).lower()}",
        "",
        "# Where fetch-satellites writes the element sets, and where a run reads them.",
        f"tle_dir = {_toml_str('~/.cache/astroskysim/tle')}",
        "",
        "# Warn when the elements are, in the median, older than this. SGP4 error grows",
        "# about a kilometre a day in LEO - arcminutes of pointing - so stale elements",
        "# put the trail in the wrong place rather than nowhere.",
        f"max_age_days = {cfg.max_age_days:g}",
        "",
        "# fetch-satellites leaves a file younger than this alone unless --force.",
        f"refetch_after_hours = {cfg.refetch_after_hours:g}",
        "",
        "# Visual magnitude at 1000 km range and 90 deg phase, for a source that does",
        "# not set its own. Everything downstream - range, phase angle, aperture, plate",
        "# scale, filter, exposure - follows from the optics, so this is the only",
        "# number describing the satellite itself.",
        f"default_std_mag = {cfg.default_std_mag:g}",
        "",
        "# Draw only satellites the Sun is actually shining on. Turning this off is a",
        "# test convenience, not a sky: nothing streaks in the Earth's shadow.",
        f"require_sunlit = {str(cfg.require_sunlit).lower()}",
        "",
        "# Cost knob. Every loaded object is propagated at every coarse step to find",
        "# the few that cross the field; the search cone grows with the step, so a",
        "# larger value is not obviously cheaper.",
        f"coarse_step_s = {cfg.coarse_step_s:g}",
        f"max_satellites = {cfg.max_satellites:d}",
        f"timeout_s = {cfg.timeout_s:g}",
        "",
        "# --- sources ---------------------------------------------------------------",
        "# One entry per element list, `enabled` being the tick box. Ticked lists are",
        "# downloaded and put in the sky; the rest are here as a menu of what else",
        "# Celestrak serves. std_mag is per list, which is coarse - it is an ordering",
        "# (an ISS pass against a Starlink pass), not photometry.",
    ]
    for src in cfg.sources:
        lines.append("")
        if src.note:
            for chunk in _wrap(src.note, 76):
                lines.append(f"# {chunk}")
        lines.append("[[sources]]")
        if src.group:
            lines.append(f"group = {_toml_str(src.group)}")
        else:
            lines.append(f"name = {_toml_str(src.key)}")
            lines.append(f"url = {_toml_str(src.url or '')}")
        lines.append(f"enabled = {str(src.enabled).lower()}")
        if src.std_mag is not None:
            lines.append(f"std_mag = {src.std_mag:g}")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    out: list[str] = []
    line = ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def discover_config(explicit: Path | None = None) -> Path | None:
    """First satellite config that exists, or ``None`` for the built-in defaults.

    An ``explicit`` path that does not exist is an error rather than a silent
    fall-through: a user who typed a path wants that file, and quietly using a
    different source list is how "my Starlink streaks vanished" happens.
    """
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"no satellite config at {path}")
        return path
    for candidate in SEARCH_PATH:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    return None


def load_satellites_config(
    ref: SatellitesRef | None = None, explicit: Path | None = None
) -> SatellitesConfig:
    """Resolve the shared config, applying the rig config's override.

    ``ref.enabled = false`` in a rig config switches satellites off for that rig
    only. The reverse - a rig config forcing them *on* - is deliberately not
    offered: with no elements downloaded there is nothing to turn on, and the
    shared file is where "this machine has satellites" is decided.
    """
    path = discover_config(explicit if explicit is not None else (ref.config if ref else None))
    if path is None:
        cfg = SatellitesConfig()
    else:
        cfg = SatellitesConfig.load(path)
        log.debug("satellite config from %s", path)
    if ref is not None and ref.enabled is False:
        cfg = cfg.model_copy(update={"enabled": False})
    return cfg


def write_default_config(path: Path | None = None) -> Path:
    """Write the template, never over an existing file."""
    target = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(default_config_text(), encoding="utf-8")
    log.info("wrote a default satellite config to %s", target)
    return target
