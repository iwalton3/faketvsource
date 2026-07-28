"""M3U playlist rendering."""

from __future__ import annotations

from .config import Config


def render(config: Config, base_url: str) -> bytes:
    """Build an M3U playlist Jellyfin's M3U tuner can import.

    tvg-id is what ties a playlist entry to an XMLTV <channel id>, so the two
    must agree for the guide to attach to the right channel.
    """
    lines = [f'#EXTM3U url-tvg="{base_url}/guide.xml" x-tvg-url="{base_url}/guide.xml"']

    for channel in config.channels:
        attrs = " ".join(
            [
                f'tvg-id="{channel.id}"',
                f'tvg-chno="{channel.number}"',
                f'tvg-name="{channel.name}"',
                f'tvg-logo="{base_url}/logo/{channel.id}.png"',
                f'group-title="{channel.group}"',
            ]
        )
        lines.append(f"#EXTINF:-1 {attrs},{channel.name}")
        lines.append(f"{base_url}/stream/{channel.id}.ts")

    return ("\n".join(lines) + "\n").encode("utf-8")
