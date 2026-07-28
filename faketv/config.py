"""Configuration loading and defaults."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace
from typing import Any

# Fonts we look for, in order, when the config does not name one. drawtext can
# also take a fontconfig name, but an explicit file is far more predictable.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)


class ConfigError(Exception):
    """Raised when the config file is unusable."""


@dataclass(frozen=True)
class Channel:
    """One fake channel."""

    id: str
    number: str
    name: str
    profile: str = "general"
    group: str = "Fake TV"
    # Accent colour used for the logo and the on-screen channel bug.
    color: str = "0x1b5e9c"
    # Audio test tone, in Hz. Giving each channel its own pitch makes it
    # obvious by ear when a client tunes to the wrong stream.
    tone: int = 440
    # Per-channel overrides of the global video settings.
    width: int | None = None
    height: int | None = None
    # Any lavfi video source: testsrc2, smptehdbars, smptebars, rgbtestsrc...
    pattern: str | None = None


@dataclass(frozen=True)
class VideoConfig:
    width: int = 1280
    height: int = 720
    fps: int = 30
    pattern: str = "testsrc2"
    bitrate: str = "3000k"
    preset: str = "veryfast"
    # Keyframe interval. Short GOPs let a client that joins mid-stream start
    # decoding quickly, which is what makes late joins feel instant.
    gop_seconds: float = 1.0


@dataclass(frozen=True)
class AudioConfig:
    bitrate: str = "128k"
    sample_rate: int = 48000
    channels: int = 2
    # Loud enough to confirm audio is flowing, quiet enough to leave running.
    volume: float = 0.12
    # "tone" = continuous sine, "beep" = one blip per second, "silence".
    mode: str = "tone"


@dataclass(frozen=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8409
    # Base URL Jellyfin should use to reach us. Leave unset to derive it from
    # each request's Host header, which is right in almost every setup.
    public_url: str | None = None
    # Everything about the guide is derived from this string, so the same seed
    # always produces the same schedule.
    seed: str = "faketv"
    guide_days: int = 5
    guide_past_hours: int = 12
    ffmpeg: str = "ffmpeg"
    font_file: str | None = None
    # Simulated tuner count: the maximum number of distinct channels that may
    # be streaming at once. 0 disables the limit.
    tuner_count: int = 0
    # Keep a channel's encoder alive this long after its last viewer leaves, so
    # a client that probes and then plays does not pay for two starts.
    stream_linger_seconds: float = 15.0
    program_images: bool = True
    video: VideoConfig = field(default_factory=VideoConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    channels: tuple[Channel, ...] = ()

    def channel_by_id(self, channel_id: str) -> Channel | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None


DEFAULT_CHANNELS = (
    Channel(
        id="fake1",
        number="1.1",
        name="Fake One HD",
        profile="general",
        color="0x1b5e9c",
        tone=440,
    ),
    Channel(
        id="fake2",
        number="2.1",
        name="Fake News 24",
        profile="news",
        color="0xb02a2a",
        tone=523,
        width=1920,
        height=1080,
        pattern="smptehdbars",
    ),
    Channel(
        id="fake3",
        number="3.1",
        name="Fake Movies",
        profile="movies",
        color="0x3d2a6b",
        tone=349,
        width=1920,
        height=1080,
    ),
    Channel(
        id="fake4",
        number="4.1",
        name="Fake Sports",
        profile="sports",
        color="0x1f7a3d",
        tone=659,
    ),
    Channel(
        id="fake5",
        number="5.1",
        name="Fake Kids",
        profile="kids",
        color="0xc7761b",
        tone=784,
        width=854,
        height=480,
    ),
    Channel(
        id="fake6",
        number="6.1",
        name="Fake Retro",
        profile="retro",
        color="0x4a4a4a",
        tone=294,
        width=640,
        height=480,
        pattern="smptebars",
    ),
)


def find_font() -> str | None:
    """Return the first usable font file, or None to fall back to fontconfig."""
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _apply(base: Any, raw: dict[str, Any], where: str) -> Any:
    """Overlay a dict of overrides onto a frozen dataclass instance."""
    known = {f.name: f.type for f in base.__dataclass_fields__.values()}
    updates: dict[str, Any] = {}
    for key, value in raw.items():
        # JSON has no comments, so treat an underscore-prefixed key as one.
        if key.startswith("_"):
            continue
        if key not in known:
            raise ConfigError(f"unknown option {where}.{key}")
        current = getattr(base, key)
        if current is not None and not isinstance(current, (str, int, float, bool)):
            raise ConfigError(f"{where}.{key} is not a simple value")
        if value is None:
            updates[key] = None
        elif isinstance(current, bool) or isinstance(value, bool):
            updates[key] = bool(value)
        elif isinstance(current, float):
            updates[key] = float(value)
        elif isinstance(current, int) and not isinstance(value, str):
            updates[key] = int(value)
        else:
            updates[key] = value
    return replace(base, **updates)


def parse_channel(raw: dict[str, Any], index: int) -> Channel:
    if not isinstance(raw, dict):
        raise ConfigError(f"channels[{index}] must be an object")
    missing = {"id", "name"} - raw.keys()
    if missing:
        raise ConfigError(f"channels[{index}] is missing {', '.join(sorted(missing))}")
    raw = dict(raw)
    raw.setdefault("number", str(index + 1))
    raw["number"] = str(raw["number"])
    raw["id"] = str(raw["id"])
    if "/" in raw["id"] or not raw["id"].strip():
        raise ConfigError(f"channels[{index}].id must be non-empty and contain no '/'")
    return _apply(Channel(id=raw["id"], number=raw["number"], name=""), raw, f"channels[{index}]")


def load(path: str | None) -> Config:
    """Load config from `path`, or return the defaults when it is None."""
    config = Config(channels=DEFAULT_CHANNELS)

    if path:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} must contain a JSON object")

        raw = dict(raw)
        video = raw.pop("video", None)
        audio = raw.pop("audio", None)
        channels = raw.pop("channels", None)

        config = _apply(config, raw, "config")
        if video is not None:
            config = replace(config, video=_apply(config.video, video, "video"))
        if audio is not None:
            config = replace(config, audio=_apply(config.audio, audio, "audio"))
        if channels is not None:
            if not isinstance(channels, list) or not channels:
                raise ConfigError("channels must be a non-empty list")
            config = replace(
                config,
                channels=tuple(parse_channel(c, i) for i, c in enumerate(channels)),
            )

    seen: set[str] = set()
    for channel in config.channels:
        if channel.id in seen:
            raise ConfigError(f"duplicate channel id {channel.id!r}")
        seen.add(channel.id)

    if config.font_file is None:
        config = replace(config, font_file=find_font())
    elif not os.path.isfile(config.font_file):
        raise ConfigError(f"font_file not found: {config.font_file}")

    if shutil.which(config.ffmpeg) is None and not os.path.isfile(config.ffmpeg):
        raise ConfigError(f"ffmpeg not found: {config.ffmpeg!r} (set 'ffmpeg' in the config)")

    if config.public_url:
        config = replace(config, public_url=config.public_url.rstrip("/"))

    return config
