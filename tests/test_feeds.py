"""Playlist, XMLTV and HDHomeRun output, checked against what Jellyfin reads."""

import unittest
from dataclasses import replace
from xml.etree import ElementTree as ET

from faketv import hdhr, playlist, xmltv
from faketv.config import DEFAULT_CHANNELS, Channel, Config
from faketv.schedule import Guide

BASE = "http://tv.example:8409"


def make_config(**overrides) -> Config:
    return replace(Config(channels=DEFAULT_CHANNELS), **overrides)


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.text = playlist.render(self.config, BASE).decode("utf-8")
        self.lines = self.text.splitlines()

    def test_starts_with_the_m3u_header(self):
        self.assertTrue(self.lines[0].startswith("#EXTM3U"))

    def test_one_extinf_and_one_url_per_channel(self):
        infos = [line for line in self.lines if line.startswith("#EXTINF:")]
        urls = [line for line in self.lines if line.startswith("http")]
        self.assertEqual(len(infos), len(self.config.channels))
        self.assertEqual(len(urls), len(self.config.channels))

    def test_urls_are_absolute_http(self):
        # Jellyfin's M3U parser skips any entry that is not an absolute
        # http/https/rtsp/rtp/udp URL.
        for line in self.lines:
            if line.startswith("http"):
                self.assertRegex(line, r"^http://tv\.example:8409/stream/\w+\.ts$")

    def test_tvg_id_matches_the_channel_id(self):
        for channel in self.config.channels:
            self.assertIn(f'tvg-id="{channel.id}"', self.text)

    def test_name_follows_the_comma(self):
        for line in self.lines:
            if line.startswith("#EXTINF:"):
                self.assertTrue(line.rsplit(",", 1)[1].strip())


class XmltvTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()
        self.guide = Guide(self.config.seed)
        self.root = ET.fromstring(xmltv.render(self.config, self.guide, BASE))

    def test_one_channel_element_per_channel(self):
        ids = [c.get("id") for c in self.root.findall("channel")]
        self.assertEqual(ids, [c.id for c in self.config.channels])

    def test_first_display_name_is_the_channel_name(self):
        for element, channel in zip(self.root.findall("channel"), self.config.channels):
            self.assertEqual(element.find("display-name").text, channel.name)

    def test_programme_channel_ids_all_resolve(self):
        ids = {c.get("id") for c in self.root.findall("channel")}
        for programme in self.root.findall("programme"):
            self.assertIn(programme.get("channel"), ids)

    def test_every_channel_has_programmes(self):
        covered = {p.get("channel") for p in self.root.findall("programme")}
        self.assertEqual(covered, {c.id for c in self.config.channels})

    def test_timestamps_carry_an_offset(self):
        # Without an explicit offset the reader has to guess a timezone.
        for programme in self.root.findall("programme")[:50]:
            for attr in ("start", "stop"):
                self.assertRegex(programme.get(attr), r"^\d{14} [+-]\d{4}$")

    def test_episode_numbers_use_xmltv_ns(self):
        systems = {
            e.get("system")
            for p in self.root.findall("programme")
            for e in p.findall("episode-num")
        }
        self.assertIn("xmltv_ns", systems)

    def test_xmltv_ns_is_zero_based(self):
        for programme in self.root.findall("programme"):
            for element in programme.findall("episode-num"):
                if element.get("system") != "xmltv_ns":
                    continue
                season, episode, _ = element.text.split(".")
                onscreen = next(
                    e.text for e in programme.findall("episode-num")
                    if e.get("system") == "onscreen"
                )
                self.assertEqual(f"S{int(season) + 1:02d}E{int(episode) + 1:02d}", onscreen)

    def test_every_programme_has_a_title(self):
        for programme in self.root.findall("programme"):
            self.assertTrue((programme.findtext("title") or "").strip())

    def test_images_can_be_turned_off(self):
        root = ET.fromstring(
            xmltv.render(make_config(program_images=False), self.guide, BASE)
        )
        self.assertEqual(root.findall("programme/image"), [])

    def test_awkward_channel_names_are_escaped(self):
        config = make_config(
            channels=(Channel(id="odd", number="9.9", name='Ampersand & "Quotes" <hi>'),)
        )
        root = ET.fromstring(xmltv.render(config, self.guide, BASE))
        self.assertEqual(root.find("channel/display-name").text, 'Ampersand & "Quotes" <hi>')


class HdHomerunTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config()

    def test_discover_advertises_transcoding(self):
        # Jellyfin only offers alternate quality streams when the model number
        # contains "hdtc".
        self.assertIn("hdtc", hdhr.discover(self.config, BASE)["ModelNumber"].lower())

    def test_discover_points_at_the_lineup(self):
        payload = hdhr.discover(self.config, BASE)
        self.assertEqual(payload["LineupURL"], f"{BASE}/lineup.json")
        self.assertEqual(payload["BaseURL"], BASE)

    def test_device_id_is_stable_for_a_seed(self):
        self.assertEqual(
            hdhr.device_id(make_config(seed="x")),
            hdhr.device_id(make_config(seed="x")),
        )
        self.assertNotEqual(
            hdhr.device_id(make_config(seed="x")),
            hdhr.device_id(make_config(seed="y")),
        )

    def test_lineup_urls_are_not_legacy(self):
        # A URL starting with "hdhomerun" would send Jellyfin down the UDP
        # control path, which we do not implement.
        for entry in hdhr.lineup(self.config, BASE):
            self.assertTrue(entry["URL"].startswith("http://"))
            self.assertFalse(entry["URL"].startswith("hdhomerun"))

    def test_lineup_marks_sd_channels(self):
        entries = {e["GuideName"]: e["HD"] for e in hdhr.lineup(self.config, BASE)}
        self.assertEqual(entries["Fake Kids"], 0)
        self.assertEqual(entries["Fake One HD"], 1)

    def test_device_xml_is_well_formed(self):
        ET.fromstring(hdhr.device_xml(self.config, BASE))


if __name__ == "__main__":
    unittest.main()
