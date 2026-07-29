"""Channel logos and programme artwork, rendered on demand by ffmpeg.

Images are generated once and cached in memory. Programme art is keyed on the
title rather than the airing, so the number of distinct images is bounded by
the size of the title pools no matter how long the guide runs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
import textwrap
import threading

from .config import DEFAULT_LOGO_STYLE, LOGO_STYLES, Channel, Config, LogoStyle
from .encoder import escape_filter_value
from .schedule import PROFILES
from .xmltv import art_key

LOG = logging.getLogger("faketv.images")

SIZES = {
    "still": (640, 360),
    "backdrop": (1280, 720),
}

# Reverse map from the hashed art key back to the title it came from.
TITLES: dict[str, str] = {
    art_key(title): title for profile in PROFILES.values() for title in profile.titles
}

PALETTE = (
    "0x1b5e9c", "0xb02a2a", "0x3d2a6b", "0x1f7a3d",
    "0xc7761b", "0x4a4a4a", "0x8c1f5a", "0x0f6b73",
)


def _colour_for(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def logo_style_for(channel: Channel) -> LogoStyle:
    return LOGO_STYLES.get(channel.logo_style, LOGO_STYLES[DEFAULT_LOGO_STYLE])


def logo_paint(channel: Channel) -> tuple[str, str]:
    """Return the (lavfi background, drawtext fontcolor) for a channel's logo.

    The background carries an explicit alpha even when it is 1.0, so the
    transparent styles differ from the opaque ones by nothing but a number.
    """
    style = logo_style_for(channel)
    fill = channel.color if style.fill == "channel" else style.fill
    return f"{fill}@{style.alpha:g}", style.text


class ImageFactory:
    """Renders and caches PNGs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._cache: dict[str, bytes] = {}

    def logo(self, channel: Channel) -> bytes | None:
        background, text_colour = logo_paint(channel)
        transparent = logo_style_for(channel).alpha < 1.0
        return self._cached(
            f"logo:{channel.id}:{channel.logo_style}",
            lambda: self._render(
                400,
                400,
                background,
                [(channel.number, 96), (channel.name, 38)],
                text_colour=text_colour,
                alpha=transparent,
            ),
        )

    def art(self, key: str, kind: str) -> bytes | None:
        if kind not in SIZES:
            return None
        title = TITLES.get(key)
        if title is None:
            return None
        width, height = SIZES[kind]
        scale = height / 360.0
        return self._cached(
            f"art:{key}:{kind}",
            lambda: self._render(
                width,
                height,
                _colour_for(key),
                [(title, int(42 * scale)), ("FAKE TV SOURCE", int(20 * scale))],
                wrap=24,
            ),
        )

    def _cached(self, key: str, build) -> bytes | None:
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            data = build()
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("could not render image %s: %s", key, exc)
            return None
        if data:
            with self._lock:
                self._cache[key] = data
        return data

    def _command(
        self,
        width: int,
        height: int,
        background: str,
        filters: list[str],
        alpha: bool,
    ) -> list[str]:
        """Build the ffmpeg argv for one still.

        `color` only carries an alpha channel if something downstream asks for
        a pixel format that has one, so a transparent background needs both the
        `format=rgba` on the source and the matching `-pix_fmt` on the encoder.
        Without either, the alpha is silently flattened onto black.
        """
        source = f"color=c={background}:s={width}x{height}"
        if alpha:
            source += ",format=rgba"
        command = [
            self.config.ffmpeg,
            "-hide_banner", "-nostdin", "-loglevel", "error",
            "-f", "lavfi",
            "-i", source,
            "-vf", ",".join(filters),
            "-frames:v", "1",
            "-c:v", "png",
        ]
        if alpha:
            command += ["-pix_fmt", "rgba"]
        return command + ["-f", "image2", "pipe:1"]

    def _render(
        self,
        width: int,
        height: int,
        background: str,
        blocks: list[tuple[str, int]],
        wrap: int = 16,
        text_colour: str = "white",
        alpha: bool = False,
    ) -> bytes:
        """Draw stacked lines of text on a background of the given colour."""
        # Text goes through files rather than the filter string so titles with
        # colons, quotes or commas cannot corrupt the filter graph.
        handles: list[str] = []
        directory = tempfile.mkdtemp(prefix="faketv-img-")
        try:
            filters = []
            total = len(blocks)
            for index, (text, size) in enumerate(blocks):
                path = os.path.join(directory, f"{index}.txt")
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(textwrap.wrap(text, wrap)) or text)
                handles.append(path)

                # Stack the blocks vertically around the centre of the frame.
                offset = (index - (total - 1) / 2) * (height * 0.22)
                sign = "+" if offset >= 0 else "-"
                options = [
                    f"textfile={escape_filter_value(path)}",
                    "expansion=none",
                    f"fontsize={size}",
                    f"fontcolor={text_colour}",
                    "line_spacing=8",
                    "x=(w-tw)/2",
                    f"y=(h-th)/2{sign}{abs(offset):.0f}",
                ]
                if self.config.font_file:
                    options.append(f"fontfile={escape_filter_value(self.config.font_file)}")
                else:
                    options.append("font=sans")
                filters.append("drawtext=" + ":".join(options))

            command = self._command(width, height, background, filters, alpha)
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode != 0 or not result.stdout:
                LOG.warning(
                    "image render failed (%s): %s",
                    result.returncode,
                    result.stderr.decode("utf-8", "replace").strip(),
                )
                return b""
            return result.stdout
        finally:
            for path in handles:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            try:
                os.rmdir(directory)
            except OSError:
                pass
