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
    return p.parse_args(argv)


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
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
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
