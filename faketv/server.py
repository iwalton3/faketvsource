"""The HTTP server: playlist, guide, streams, artwork and HDHomeRun endpoints."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import sys
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import __version__, hdhr, playlist, xmltv
from .config import Config
from .encoder import StreamManager, TunerBusy
from .images import ImageFactory
from .schedule import Guide

LOG = logging.getLogger("faketv.server")

# Give up on a client that has taken nothing from its queue for this long. A
# healthy viewer sees data continuously, so silence means the socket is gone.
STREAM_IDLE_TIMEOUT = 30.0


class App:
    """Everything the request handlers need, built once at startup."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.guide = Guide(config.seed)
        self.streams = StreamManager(config, self.guide)
        self.images = ImageFactory(config)

    def shutdown(self) -> None:
        self.streams.shutdown()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"faketvsource/{__version__}"
    sys_version = ""
    timeout = 30

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        LOG.info("%s %s", self.address_string(), format % args)

    def log_error(self, format: str, *args) -> None:  # noqa: A002
        LOG.debug("%s %s", self.address_string(), format % args)

    def base_url(self) -> str:
        """The URL a client should use to reach us.

        Derived from the request's Host header so the playlist works whether
        you reach the server by hostname, LAN address or through a tunnel.
        """
        if self.app.config.public_url:
            return self.app.config.public_url
        host = self.headers.get("Host")
        if not host:
            host = f"{self.server.server_address[0]}:{self.server.server_address[1]}"
        return f"http://{host}"

    def _send(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        send_body: bool = True,
        cache: str = "no-cache",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _json(self, payload, status: int = 200, send_body: bool = True) -> None:
        body = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        self._send(body, "application/json", status, send_body)

    def _error(self, status: int, message: str, send_body: bool = True) -> None:
        self._send(
            f"{status} {message}\n".encode("utf-8"),
            "text/plain; charset=utf-8",
            status,
            send_body,
        )

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._route(send_body=True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._route(send_body=False)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path == "/lineup.post":
            # Jellyfin pokes this to kick off a channel scan; there is nothing
            # to scan, so just say it worked.
            self._send(b"", "application/json")
            return
        self._error(404, "Not Found")

    def _route(self, send_body: bool) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        config = self.app.config

        try:
            if path == "/":
                self._send(self._index_page(), "text/html; charset=utf-8", send_body=send_body)

            elif path in ("/playlist.m3u", "/playlist", "/channels.m3u", "/m3u"):
                body = playlist.render(config, self.base_url())
                self._send(body, "audio/x-mpegurl", send_body=send_body)

            elif path in ("/guide.xml", "/xmltv.xml", "/epg.xml"):
                body = xmltv.render(config, self.app.guide, self.base_url())
                self._send(body, "application/xml", send_body=send_body)

            elif path.startswith("/stream/"):
                self._stream(path[len("/stream/"):], query, send_body)

            elif path.startswith("/logo/"):
                self._logo(path[len("/logo/"):], send_body)

            elif path.startswith("/art/"):
                self._art(path[len("/art/"):], send_body)

            elif path == "/discover.json":
                self._json(hdhr.discover(config, self.base_url()), send_body=send_body)

            elif path == "/lineup.json":
                self._json(hdhr.lineup(config, self.base_url()), send_body=send_body)

            elif path == "/lineup_status.json":
                self._json(hdhr.lineup_status(), send_body=send_body)

            elif path == "/device.xml":
                body = hdhr.device_xml(config, self.base_url())
                self._send(body, "application/xml", send_body=send_body)

            elif path == "/status.json":
                self._json(
                    {
                        "version": __version__,
                        "channels": len(config.channels),
                        "tuner_count": config.tuner_count,
                        "active": self.app.streams.status(),
                    },
                    send_body=send_body,
                )

            elif path == "/now.json":
                self._json(self._now_payload(), send_body=send_body)

            else:
                self._error(404, "Not Found", send_body)

        except (BrokenPipeError, ConnectionResetError):
            LOG.debug("client disconnected during %s", path)

    # -- endpoints --------------------------------------------------------

    def _stream(self, name: str, query: dict[str, list[str]], send_body: bool) -> None:
        channel_id = name[:-3] if name.endswith(".ts") else name
        channel = self.app.config.channel_by_id(channel_id)
        if channel is None:
            self._error(404, "No such channel", send_body)
            return

        # HDHomeRun clients ask for a quality by appending ?transcode=<profile>.
        profile = (query.get("transcode") or [None])[0]

        if not send_body:
            # Jellyfin HEADs the stream URL to decide whether the response is
            # MPEG-TS and therefore shareable between viewers, so the content
            # type has to be right even with no body.
            self.send_response(200)
            self.send_header("Content-Type", "video/MP2T")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        try:
            subscriber = self.app.streams.open(channel, profile)
        except TunerBusy as exc:
            LOG.warning("refusing %s: %s", channel_id, exc)
            self.send_response(503)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            body = f"{exc}\n".encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Retry-After", "10")
            self.end_headers()
            self.wfile.write(body)
            return

        LOG.info("tuned %s -> %s", self.address_string(), subscriber.channel_id)
        # The stream never ends, so there is no length to declare; the response
        # runs until one side closes the connection.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "video/MP2T")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        idle = 0.0
        try:
            while True:
                chunk = subscriber.read(timeout=5.0)
                if chunk is None:
                    break
                if not chunk:
                    idle += 5.0
                    if idle >= STREAM_IDLE_TIMEOUT:
                        LOG.warning("no data for %s, closing", subscriber.channel_id)
                        break
                    continue
                idle = 0.0
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            self.app.streams.close(subscriber)
            LOG.info(
                "closed %s (%d bytes, %d chunks dropped)",
                subscriber.channel_id,
                subscriber.sent,
                subscriber.dropped,
            )

    def _logo(self, name: str, send_body: bool) -> None:
        channel_id = name[:-4] if name.endswith(".png") else name
        channel = self.app.config.channel_by_id(channel_id)
        if channel is None:
            self._error(404, "No such channel", send_body)
            return
        data = self.app.images.logo(channel)
        if not data:
            self._error(500, "Could not render logo", send_body)
            return
        self._send(data, "image/png", send_body=send_body, cache="public, max-age=86400")

    def _art(self, rest: str, send_body: bool) -> None:
        bits = rest.split("/")
        if len(bits) != 2:
            self._error(404, "Not Found", send_body)
            return
        key, kind = bits[0], bits[1]
        if kind.endswith(".png"):
            kind = kind[:-4]
        data = self.app.images.art(key, kind)
        if not data:
            self._error(404, "No such image", send_body)
            return
        self._send(data, "image/png", send_body=send_body, cache="public, max-age=86400")

    def _now_payload(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        channels = []
        for channel in self.app.config.channels:
            current, upcoming = self.app.guide.now_and_next(channel.id, channel.profile, now)
            channels.append(
                {
                    "id": channel.id,
                    "number": channel.number,
                    "name": channel.name,
                    "now": _programme_json(current),
                    "next": _programme_json(upcoming),
                }
            )
        return {"time": now.isoformat(), "channels": channels}

    # -- landing page -----------------------------------------------------

    def _index_page(self) -> bytes:
        base = self.base_url()
        config = self.app.config
        now = datetime.now(timezone.utc)

        rows = []
        for channel in config.channels:
            current, upcoming = self.app.guide.now_and_next(channel.id, channel.profile, now)
            size = f"{channel.width or config.video.width}x{channel.height or config.video.height}"
            rows.append(
                "<tr>"
                f'<td><img class=logo src="/logo/{escape(channel.id)}.png" alt=""></td>'
                f"<td class=num>{escape(channel.number)}</td>"
                f"<td><strong>{escape(channel.name)}</strong><br>"
                f"<span class=dim>{escape(channel.id)} &middot; {escape(channel.profile)} &middot; {size}"
                f" &middot; {escape(channel.logo_style)} logo</span></td>"
                f"<td>{escape(current.title) if current else '&mdash;'}"
                f"<br><span class=dim>{f'{current.start:%H:%M}-{current.stop:%H:%M} UTC' if current else ''}</span></td>"
                f"<td>{escape(upcoming.title) if upcoming else '&mdash;'}</td>"
                f'<td><a href="/stream/{escape(channel.id)}.ts">stream</a></td>'
                "</tr>"
            )

        urls = [
            ("M3U playlist (M3U Tuner)", f"{base}/playlist.m3u"),
            ("XMLTV guide (XMLTV listings)", f"{base}/guide.xml"),
            ("HDHomeRun device (HD HomeRun tuner)", base),
        ]
        url_rows = "".join(
            f"<tr><td>{escape(label)}</td><td><code>{escape(url)}</code></td></tr>"
            for label, url in urls
        )

        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fake TV Source</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 60rem; padding: 2rem 1rem; }}
 h1 {{ margin: 0 0 .25rem; font-size: 1.6rem; }}
 p.sub {{ margin-top: 0; opacity: .7; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
 th, td {{ text-align: left; padding: .5rem .6rem; border-bottom: 1px solid rgba(128,128,128,.3); vertical-align: top; }}
 th {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; opacity: .6; }}
 img {{ width: 44px; height: 44px; border-radius: 6px; display: block; }}
 /* Most of the logos are transparent, half of them with black text and half
    with white, so a surface that is either colour hides one of them. This one
    is a mid-tone alpha checker, which is what a client ought to be doing. */
 img.logo {{
   background-color: #808080;
   background-image:
     linear-gradient(45deg, #6f6f6f 25%, transparent 25%, transparent 75%, #6f6f6f 75%),
     linear-gradient(45deg, #6f6f6f 25%, transparent 25%, transparent 75%, #6f6f6f 75%);
   background-size: 14px 14px;
   background-position: 0 0, 7px 7px;
 }}
 code {{ font-size: .9em; word-break: break-all; }}
 .num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
 .dim {{ opacity: .6; font-size: .85em; }}
</style></head><body>
<h1>Fake TV Source</h1>
<p class=sub>{len(config.channels)} fake channels &middot; guide covers {config.guide_days} days &middot; server time {now:%Y-%m-%d %H:%M:%S} UTC</p>

<h2>Point Jellyfin at</h2>
<table><tbody>{url_rows}</tbody></table>

<h2>Channels</h2>
<p class=dim>Logos are deliberately awkward: most have a transparent background,
some with white text and some with black, so a client that composites them onto
the wrong colour ends up with an invisible logo. They are shown here on a
mid-tone checker, where every style stays legible.</p>
<table>
<thead><tr><th></th><th>No.</th><th>Channel</th><th>Now</th><th>Next</th><th></th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>

<p class=dim>Also serving
<a href="/status.json">/status.json</a>,
<a href="/now.json">/now.json</a>,
<a href="/discover.json">/discover.json</a>,
<a href="/lineup.json">/lineup.json</a>.</p>
</body></html>
"""
        return html.encode("utf-8")


def _programme_json(programme) -> dict[str, object] | None:
    if programme is None:
        return None
    return {
        "title": programme.title,
        "sub_title": programme.sub_title,
        "start": programme.start.isoformat(),
        "stop": programme.stop.isoformat(),
        "categories": list(programme.categories),
        "season": programme.season,
        "episode": programme.episode,
        "is_live": programme.is_live,
        "is_new": programme.is_new,
    }


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config, app: App) -> None:
        if ":" in config.host:
            self.address_family = socket.AF_INET6
        super().__init__((config.host, config.port), Handler)
        self.app = app

    def handle_error(self, request, client_address) -> None:
        # A client hanging up mid-stream is normal, not worth a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            LOG.debug("connection from %s dropped: %s", client_address, exc)
            return
        socketserver.BaseServer.handle_error(self, request, client_address)
