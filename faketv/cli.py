"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import socket
import sys
from dataclasses import replace

from . import __version__, playlist, xmltv
from .config import Config, ConfigError, load
from .schedule import Guide
from .server import App, Server

LOG = logging.getLogger("faketv")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faketv",
        description="A fake Live TV source for testing Jellyfin clients.",
    )
    parser.add_argument("-c", "--config", help="path to a JSON config file")
    parser.add_argument("--host", help="address to bind (default 0.0.0.0)")
    parser.add_argument("--port", type=int, help="port to bind (default 8409)")
    parser.add_argument(
        "--public-url",
        help="base URL Jellyfin should use, if it differs from the request Host",
    )
    parser.add_argument("--seed", help="schedule seed; the same seed gives the same guide")
    parser.add_argument("--tuner-count", type=int, help="simulated tuner limit (0 = unlimited)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"faketvsource {__version__}")

    dump = parser.add_mutually_exclusive_group()
    dump.add_argument(
        "--print-playlist",
        action="store_true",
        help="write the M3U playlist to stdout and exit",
    )
    dump.add_argument(
        "--print-guide",
        action="store_true",
        help="write the XMLTV guide to stdout and exit",
    )
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    updates = {}
    if args.host:
        updates["host"] = args.host
    if args.port:
        updates["port"] = args.port
    if args.public_url:
        updates["public_url"] = args.public_url.rstrip("/")
    if args.seed:
        updates["seed"] = args.seed
    if args.tuner_count is not None:
        updates["tuner_count"] = args.tuner_count
    return replace(config, **updates) if updates else config


def local_address() -> str:
    """Best guess at the LAN address other machines can reach us on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # No packets are sent; this just asks the routing table which source
        # address would be used to reach the outside world.
        probe.connect(("192.0.2.1", 9))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _raise_interrupt(signum, frame) -> None:
    """Turn a signal into the exception serve_forever already unwinds on."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # The per-request access log is noise once things work.
        logging.getLogger("faketv.server").setLevel(logging.WARNING)

    try:
        config = apply_overrides(load(args.config), args)
    except ConfigError as exc:
        print(f"faketv: {exc}", file=sys.stderr)
        return 2

    if args.print_playlist or args.print_guide:
        base = config.public_url or f"http://{local_address()}:{config.port}"
        if args.print_playlist:
            sys.stdout.buffer.write(playlist.render(config, base))
        else:
            sys.stdout.buffer.write(xmltv.render(config, Guide(config.seed), base))
        return 0

    app = App(config)
    try:
        server = Server(config, app)
    except OSError as exc:
        print(f"faketv: cannot bind {config.host}:{config.port}: {exc}", file=sys.stderr)
        app.shutdown()
        return 1

    base = config.public_url or f"http://{local_address()}:{config.port}"
    LOG.info("faketvsource %s serving %d channels", __version__, len(config.channels))
    LOG.info("  web ui / HDHomeRun device  %s", base)
    LOG.info("  M3U playlist               %s/playlist.m3u", base)
    LOG.info("  XMLTV guide                %s/guide.xml", base)
    if config.font_file:
        LOG.debug("  overlay font               %s", config.font_file)
    else:
        LOG.warning("no font file found; falling back to fontconfig 'sans'")

    # systemd and docker stop send SIGTERM, so route it into the same path as
    # Ctrl-C; otherwise the encoders and their temp files are left behind.
    signal.signal(signal.SIGTERM, _raise_interrupt)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("shutting down")
    finally:
        server.shutdown()
        server.server_close()
        app.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
