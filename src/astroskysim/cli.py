"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
from pathlib import Path

from .config import Config, SourceMode
from .devices.camera import Camera, GuideCamera
from .devices.filterwheel import FilterWheel
from .devices.focuser import Focuser
from .devices.mount import Mount
from .devices.rotator import Rotator
from .devices.weather import Weather
from .indi.server import IndiServer
from .rig import build_rig
from .satellites.config import (
    DEFAULT_CONFIG_PATH,
    SatellitesConfig,
    discover_config,
    write_default_config,
)
from .satellites.tle import fetch_sources, parse_tle_text, tle_path
from .sky.fetch import DEFAULT_RELEASE, RELEASES, fetch_catalog, resolve_release

log = logging.getLogger("astroskysim")


def build_server(cfg: Config) -> IndiServer:
    rig = build_rig(cfg)
    server = IndiServer(rig, host=cfg.server.host, port=cfg.server.port)
    s = cfg.server
    for enabled, cls in (
        (s.mount, Mount),
        (s.camera, Camera),
        (s.guide_camera, GuideCamera),
        (s.focuser, Focuser),
        (s.rotator, Rotator),
        (s.filter_wheel, FilterWheel),
        (s.weather, Weather),
    ):
        if enabled:
            server.add_device(cls(rig))
    if not server.devices:
        raise SystemExit("no devices enabled - nothing to serve")
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="astroskysim",
        description="Headless INDI telescope, camera, focuser, rotator and filter wheel simulator.",
    )
    p.add_argument("-c", "--config", type=Path, help="TOML config file")
    p.add_argument("-p", "--port", type=int, help="override the listen port")
    p.add_argument("--host", help="override the listen address")
    p.add_argument(
        "-m",
        "--mode",
        choices=[m.value for m in SourceMode],
        help="image source: artificial stars, DSS survey, or the two composited",
    )
    p.add_argument(
        "--survey",
        help="survey for dss/composite: hips:<id or short name> (e.g. hips:dss2r, "
        "hips:ha, hips:CDS/P/PanSTARRS/DR1/r), 'skyview:DSS2 Red' or eso:DSS",
    )
    p.add_argument("--catalog-dir", type=Path, help="directory holding the .290 star database")
    p.add_argument(
        "--satellites",
        type=Path,
        metavar="FILE",
        help="shared satellite config (default: ./satellites.toml, then "
        f"{DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")

    # Optional subcommand: a bare `astroskysim -c sim.toml` still runs the server,
    # so nothing that worked before needs the word "serve" added to it.
    sub = p.add_subparsers(dest="command")
    f = sub.add_parser(
        "fetch-catalog",
        help="download the .290 star database into the catalogue directory",
        description="Download and unpack a HNSKY .290 star database. Idempotent: does "
        "nothing if the cells are already there.",
    )
    f.add_argument(
        "release",
        nargs="?",
        default=DEFAULT_RELEASE,
        choices=sorted(RELEASES),
        help=f"which database (default: {DEFAULT_RELEASE})",
    )
    f.add_argument(
        "-d",
        "--dest",
        type=Path,
        help="where to unpack (default: source.artificial.catalog_dir from -c, else ./catalog)",
    )
    f.add_argument("--url", help="override the download URL, e.g. an unpublished mirror")
    f.add_argument("--sha256", help="expected archive digest, overriding the pinned one")
    f.add_argument("--force", action="store_true", help="re-download even if the cells are present")
    f.add_argument("--timeout", type=float, default=300.0, metavar="S", help="download timeout")
    # default=None, not 0: a subparser default overwrites whatever the main parser
    # already put in the same dest, so `astroskysim -v fetch-catalog` would lose
    # its -v. None means "not given here" and main() falls back.
    f.add_argument("-v", "--verbose", action="count", default=None, dest="sub_verbose")

    s = sub.add_parser(
        "fetch-satellites",
        help="download orbital elements for the enabled satellite sources",
        description="Download two-line element sets from Celestrak into the shared "
        "element cache. Idempotent: a source fetched within refetch_after_hours is "
        "left alone. Writes a default satellite config if none is found yet.",
    )
    s.add_argument(
        "--force", action="store_true", help="re-download even if the cached elements are fresh"
    )
    s.add_argument(
        "--all",
        action="store_true",
        help="fetch every source in the config, not just the enabled ones",
    )
    s.add_argument(
        "-l", "--list", action="store_true", dest="list_sources",
        help="show the sources and the age of their elements, and download nothing",
    )
    s.add_argument("--timeout", type=float, metavar="S", help="download timeout per source")
    # Accepted on both sides of the subcommand, because typing it after the verb
    # is the obvious thing to do. Same dest trick as -v: a subparser default
    # overwrites the main parser's value, so it needs its own.
    s.add_argument("--satellites", type=Path, dest="sub_satellites", default=None, metavar="FILE")
    s.add_argument("-v", "--verbose", action="count", default=None, dest="sub_verbose")

    args = p.parse_args(argv)
    if getattr(args, "sub_verbose", None):
        args.verbose = args.sub_verbose
    if getattr(args, "sub_satellites", None):
        args.satellites = args.sub_satellites
    return args


def load_config(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config)
    if args.port is not None:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host
    if args.mode:
        cfg.source.mode = SourceMode(args.mode)
    if args.survey:
        cfg.source.dss.survey = args.survey
    if args.catalog_dir:
        cfg.source.artificial.catalog_dir = args.catalog_dir
    if args.satellites:
        cfg.satellites.config = args.satellites
    return cfg


def cmd_fetch_catalog(args: argparse.Namespace) -> int:
    """``astroskysim fetch-catalog`` — populate the catalogue directory."""
    try:
        release = resolve_release(args.release, args.url, args.sha256)
    except KeyError as exc:
        print(f"astroskysim: {exc}", file=sys.stderr)
        return 2

    dest = args.dest or args.catalog_dir
    if dest is None and args.config:
        # Honour the config so the download lands where the run will look for it.
        try:
            dest = Config.load(args.config).source.artificial.catalog_dir
        except Exception as exc:
            print(f"astroskysim: configuration error: {exc}", file=sys.stderr)
            return 2
    dest = dest or Path("catalog")

    try:
        fetch_catalog(release, dest, force=args.force, timeout_s=args.timeout)
    except (RuntimeError, OSError) as exc:
        print(f"astroskysim: {exc}", file=sys.stderr)
        return 1
    return 0


def _satellites_config(args: argparse.Namespace) -> tuple[SatellitesConfig, Path | None]:
    """The shared satellite config the CLI should act on, and where it came from.

    ``-c`` is honoured so ``fetch-satellites -c sim.toml`` downloads into the
    directory that run will read from, the same way ``fetch-catalog`` does.
    """
    ref = None
    if args.config and args.satellites is None:
        ref = Config.load(args.config).satellites
    path = discover_config(args.satellites or (ref.config if ref else None))
    cfg = SatellitesConfig.load(path) if path else SatellitesConfig()
    return cfg, path


def cmd_fetch_satellites(args: argparse.Namespace) -> int:
    """``astroskysim fetch-satellites`` — populate the element cache."""
    try:
        cfg, path = _satellites_config(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"astroskysim: {exc}", file=sys.stderr)
        return 2

    if path is None and not args.list_sources:
        # Give the user the menu to edit. Never overwrites: write_default_config
        # returns an existing file untouched.
        path = write_default_config()
        cfg = SatellitesConfig.load(path)
    print(f"satellite config: {path or 'built-in defaults'}", file=sys.stderr)

    if args.timeout:
        cfg = cfg.model_copy(update={"timeout_s": args.timeout})

    if args.list_sources:
        for src in cfg.sources:
            cached = tle_path(src, cfg.tle_dir)
            if cached.is_file():
                age_d = (time.time() - cached.stat().st_mtime) / 86400.0
                n = len(parse_tle_text(cached.read_text(errors="replace")))
                state = f"{n:6d} objects, {age_d:.1f} d old"
            else:
                state = "not fetched"
            print(f"  [{'x' if src.enabled else ' '}] {src.key:<22} {state}")
        return 0

    results = fetch_sources(cfg, force=args.force, sources=cfg.sources if args.all else None)
    failed = [r for r in results if r.status == "failed"]
    for r in failed:
        print(f"astroskysim: {r.source}: {r.detail}", file=sys.stderr)
    total = sum(r.count for r in results)
    print(
        f"{total} objects across {len(results) - len(failed)} of {len(results)} sources "
        f"in {cfg.tle_dir}",
        file=sys.stderr,
    )
    # A partial failure still leaves a usable sky, so it is not an error exit
    # unless nothing at all came back.
    return 1 if results and len(failed) == len(results) else 0


async def run(cfg: Config) -> None:
    server = build_server(cfg)
    try:
        await server.serve_forever(tick_hz=cfg.server.tick_hz)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # A download that prints nothing looks like a hang, so the fetch commands
    # start at info rather than warning.
    verbosity = (
        max(args.verbose, 1)
        if args.command in ("fetch-catalog", "fetch-satellites")
        else args.verbose
    )
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(verbosity, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command == "fetch-catalog":
        return cmd_fetch_catalog(args)
    if args.command == "fetch-satellites":
        return cmd_fetch_satellites(args)

    try:
        cfg = load_config(args)
    except Exception as exc:
        print(f"astroskysim: configuration error: {exc}", file=sys.stderr)
        return 2

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
