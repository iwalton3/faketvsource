"""XMLTV guide rendering."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from .config import Channel, Config
from .schedule import Guide, Programme

XMLTV_TIME = "%Y%m%d%H%M%S %z"


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(XMLTV_TIME)


def art_key(title: str) -> str:
    """A short stable key for a programme's artwork.

    Keyed on the title rather than the airing so that every episode of a show
    shares one image; that bounds how many images the server has to render.
    """
    return hashlib.sha256(title.encode("utf-8")).hexdigest()[:12]


def _text(parent: ET.Element, tag: str, value: str, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    element.text = value
    return element


def _channel_element(root: ET.Element, channel: Channel, base_url: str) -> None:
    element = ET.SubElement(root, "channel", {"id": channel.id})
    # Jellyfin takes the first display-name as the channel name.
    _text(element, "display-name", channel.name)
    _text(element, "display-name", channel.number)
    _text(element, "display-name", f"{channel.number} {channel.name}")
    _text(element, "lcn", channel.number)
    ET.SubElement(element, "icon", {"src": f"{base_url}/logo/{channel.id}.png"})
    _text(element, "url", f"{base_url}/stream/{channel.id}.ts")


def _programme_element(
    root: ET.Element,
    programme: Programme,
    base_url: str,
    with_images: bool,
) -> None:
    element = ET.SubElement(
        root,
        "programme",
        {
            "start": _stamp(programme.start),
            "stop": _stamp(programme.stop),
            "channel": programme.channel_id,
        },
    )

    _text(element, "title", programme.title, lang="en")
    if programme.sub_title:
        _text(element, "sub-title", programme.sub_title, lang="en")
    _text(element, "desc", programme.desc, lang="en")

    for category in programme.categories:
        _text(element, "category", category, lang="en")

    if programme.season is not None and programme.episode is not None:
        # xmltv_ns counts from zero and encodes season.episode.part
        _text(
            element,
            "episode-num",
            f"{programme.season - 1}.{programme.episode - 1}.0/1",
            system="xmltv_ns",
        )
        _text(
            element,
            "episode-num",
            f"S{programme.season:02d}E{programme.episode:02d}",
            system="onscreen",
        )

    if programme.year is not None:
        _text(element, "date", str(programme.year))

    if with_images:
        key = art_key(programme.title)
        ET.SubElement(element, "icon", {"src": f"{base_url}/art/{key}/still.png"})
        _text(element, "image", f"{base_url}/art/{key}/still.png", type="still")
        _text(element, "image", f"{base_url}/art/{key}/backdrop.png", type="backdrop")

    if programme.rating:
        rating = ET.SubElement(element, "rating", {"system": "VCHIP"})
        _text(rating, "value", programme.rating)

    if programme.star_rating is not None:
        stars = ET.SubElement(element, "star-rating")
        _text(stars, "value", f"{programme.star_rating}/10")

    if programme.is_live:
        ET.SubElement(element, "live")
    if programme.is_premiere:
        ET.SubElement(element, "premiere")
    if programme.is_new:
        ET.SubElement(element, "new")
    if programme.previously_shown:
        ET.SubElement(element, "previously-shown")


def render(config: Config, guide: Guide, base_url: str, now: datetime | None = None) -> bytes:
    """Build the full XMLTV document for every configured channel."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(hours=config.guide_past_hours)
    end = now + timedelta(days=config.guide_days)

    root = ET.Element(
        "tv",
        {
            "generator-info-name": "faketvsource",
            "generator-info-url": base_url,
            "source-info-name": "Fake TV Source",
        },
    )

    for channel in config.channels:
        _channel_element(root, channel, base_url)

    for channel in config.channels:
        for programme in guide.between(channel.id, channel.profile, start, end):
            _programme_element(root, programme, base_url, config.program_images)

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return body + b"\n"
