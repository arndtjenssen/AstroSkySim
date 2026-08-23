"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from .config import Config, SourceMode
from .devices.camera import Camera, GuideCamera
from .devices.filterwheel import FilterWheel
from .devices.focuser import Focuser
from .devices.mount import Mount
from .devices.rotator import Rotator
from .indi.server import IndiServer
from .rig import build_rig
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

    args = p.parse_args(argv)
    if getattr(args, "sub_verbose", None):
        args.verbose = args.sub_verbose
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
    # A 56 MB download that prints nothing looks like a hang, so fetch-catalog
    # starts at info rather than warning.
    verbosity = max(args.verbose, 1) if args.command == "fetch-catalog" else args.verbose
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(verbosity, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command == "fetch-catalog":
        return cmd_fetch_catalog(args)

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
