"""HDHomeRun tuner emulation.

Jellyfin's HDHomeRun tuner host is a different code path from its M3U tuner —
different media sources, different stream handling, its own tuner accounting —
so pretending to be one lets you test that path without owning the hardware.

Only the HTTP side of the protocol is implemented, which is all a modern HDHR
needs: discover.json, lineup.json and plain HTTP streams. Add the tuner in
Jellyfin by URL; broadcast discovery on UDP 65001 is not emulated because
Jellyfin assumes a discovered device answers on port 80.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .config import Config

# Jellyfin decides a device can transcode by looking for "hdtc" in the model
# number, which is what unlocks the alternate quality media sources.
MODEL_NUMBER = "HDTC-2US"
FIRMWARE_NAME = "hdhomerun_faketv"


def device_id(config: Config) -> str:
    """A stable pseudo-device id derived from the config seed."""
    digest = hashlib.sha256(f"hdhr|{config.seed}".encode("utf-8")).hexdigest()
    return digest[:8].upper()


def discover(config: Config, base_url: str) -> dict[str, Any]:
    return {
        "FriendlyName": "Fake TV Source",
        "ModelNumber": MODEL_NUMBER,
        "FirmwareName": FIRMWARE_NAME,
        "FirmwareVersion": "20260101",
        "DeviceID": device_id(config),
        "DeviceAuth": "faketvsource",
        "BaseURL": base_url,
        "LineupURL": f"{base_url}/lineup.json",
        # What we advertise as tuners. The real limit is `tuner_count`; when
        # that is unlimited, claim a plausible number so the UI has something.
        "TunerCount": config.tuner_count or len(config.channels),
    }


def lineup(config: Config, base_url: str) -> list[dict[str, Any]]:
    entries = []
    for channel in config.channels:
        height = channel.height or config.video.height
        entries.append(
            {
                "GuideNumber": channel.number,
                "GuideName": channel.name,
                "VideoCodec": "H264",
                "AudioCodec": "AAC",
                "URL": f"{base_url}/stream/{channel.id}.ts",
                "HD": 1 if height >= 720 else 0,
                "Favorite": 0,
                "DRM": 0,
            }
        )
    return entries


def lineup_status() -> dict[str, Any]:
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


def device_xml(config: Config, base_url: str) -> bytes:
    """The UPnP description document a real HDHomeRun serves."""
    uuid = device_id(config)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <URLBase>{base_url}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Fake TV Source</friendlyName>
    <manufacturer>Silicondust</manufacturer>
    <modelName>{MODEL_NUMBER}</modelName>
    <modelNumber>{MODEL_NUMBER}</modelNumber>
    <serialNumber>{uuid}</serialNumber>
    <UDN>uuid:{uuid}-0000-0000-0000-{uuid.lower()}</UDN>
  </device>
</root>
"""
    return body.encode("utf-8")
