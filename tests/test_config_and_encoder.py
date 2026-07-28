"""Config parsing and the ffmpeg command we build from it."""

import json
import os
import tempfile
import unittest
from dataclasses import replace

from faketv.config import DEFAULT_CHANNELS, Channel, Config, ConfigError, load
from faketv.encoder import TRANSCODE_PROFILES, Broadcaster, Subscriber, escape_filter_value


def write_config(payload: dict) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(payload, handle)
    handle.close()
    return handle.name


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.paths = []

    def tearDown(self):
        for path in self.paths:
            os.unlink(path)

    def load(self, payload: dict) -> Config:
        path = write_config(payload)
        self.paths.append(path)
        return load(path)

    def test_defaults_without_a_file(self):
        config = load(None)
        self.assertEqual(config.channels, DEFAULT_CHANNELS)
        self.assertEqual(config.port, 8409)

    def test_overrides_are_applied(self):
        config = self.load({"port": 9000, "seed": "other", "guide_days": 2})
        self.assertEqual(config.port, 9000)
        self.assertEqual(config.seed, "other")
        self.assertEqual(config.guide_days, 2)

    def test_nested_sections_merge_rather_than_replace(self):
        config = self.load({"video": {"width": 640}})
        self.assertEqual(config.video.width, 640)
        # Untouched keys keep their defaults.
        self.assertEqual(config.video.fps, Config().video.fps)

    def test_underscore_keys_are_comments(self):
        config = self.load({"_note": "ignore me", "port": 1234})
        self.assertEqual(config.port, 1234)

    def test_unknown_keys_are_rejected(self):
        with self.assertRaises(ConfigError):
            self.load({"prot": 8409})

    def test_channels_replace_the_defaults(self):
        config = self.load({"channels": [{"id": "a", "name": "A"}]})
        self.assertEqual(len(config.channels), 1)
        self.assertEqual(config.channels[0].id, "a")
        self.assertEqual(config.channels[0].number, "1")

    def test_channel_needs_an_id_and_a_name(self):
        with self.assertRaises(ConfigError):
            self.load({"channels": [{"name": "no id"}]})

    def test_channel_ids_must_be_unique(self):
        with self.assertRaises(ConfigError):
            self.load({"channels": [{"id": "a", "name": "A"}, {"id": "a", "name": "B"}]})

    def test_channel_id_may_not_contain_a_slash(self):
        # The id becomes a URL path segment.
        with self.assertRaises(ConfigError):
            self.load({"channels": [{"id": "a/b", "name": "A"}]})

    def test_numeric_channel_numbers_become_strings(self):
        config = self.load({"channels": [{"id": "a", "name": "A", "number": 7}]})
        self.assertEqual(config.channels[0].number, "7")

    def test_missing_file_is_reported(self):
        with self.assertRaises(ConfigError):
            load("/nonexistent/faketv.json")

    def test_broken_json_is_reported(self):
        path = write_config({})
        self.paths.append(path)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(ConfigError):
            load(path)

    def test_lookup_by_id(self):
        config = load(None)
        self.assertIsNotNone(config.channel_by_id("fake1"))
        self.assertIsNone(config.channel_by_id("nope"))


class EscapingTests(unittest.TestCase):
    def test_filter_metacharacters_are_escaped(self):
        self.assertEqual(escape_filter_value("/a:b"), "/a\\:b")
        self.assertEqual(escape_filter_value("x,y"), "x\\,y")
        self.assertEqual(escape_filter_value("a'b"), "a\\'b")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="faketv-test-")
        self.config = Config(channels=DEFAULT_CHANNELS, font_file=None)

    def tearDown(self):
        for name in os.listdir(self.directory):
            os.unlink(os.path.join(self.directory, name))
        os.rmdir(self.directory)

    def build(self, channel: Channel, profile=None, config=None) -> list[str]:
        return Broadcaster(config or self.config, channel, self.directory, profile).command()

    def test_command_writes_mpegts_to_stdout(self):
        command = self.build(DEFAULT_CHANNELS[0])
        self.assertEqual(command[-1], "pipe:1")
        self.assertIn("mpegts", command)

    def test_channel_overrides_the_global_size(self):
        kids = next(c for c in DEFAULT_CHANNELS if c.id == "fake5")
        command = self.build(kids)
        self.assertIn(f"size={kids.width}x{kids.height}", " ".join(command))

    def test_transcode_profile_overrides_the_channel_size(self):
        width, height, bitrate = TRANSCODE_PROFILES["internet240"]
        command = self.build(DEFAULT_CHANNELS[0], "internet240")
        self.assertIn(f"size={width}x{height}", " ".join(command))
        self.assertIn(bitrate, command)

    def test_unknown_profile_falls_back_to_native(self):
        broadcaster = Broadcaster(self.config, DEFAULT_CHANNELS[0], self.directory, "bogus")
        self.assertIsNone(broadcaster.profile)
        self.assertEqual(broadcaster.key, "fake1")

    def test_each_channel_gets_its_own_tone(self):
        one = " ".join(self.build(DEFAULT_CHANNELS[0]))
        two = " ".join(self.build(DEFAULT_CHANNELS[1]))
        self.assertIn(f"frequency={DEFAULT_CHANNELS[0].tone}", one)
        self.assertIn(f"frequency={DEFAULT_CHANNELS[1].tone}", two)

    def test_silence_mode_uses_a_null_source(self):
        config = replace(self.config, audio=replace(self.config.audio, mode="silence"))
        self.assertIn("anullsrc", " ".join(self.build(DEFAULT_CHANNELS[0], config=config)))

    def test_hostile_channel_name_cannot_escape_the_filter_graph(self):
        # Names reach ffmpeg through a text file, never through the filter
        # string, so filter metacharacters in a name are harmless.
        nasty = Channel(id="evil", number="0", name="a:b,c'd[e]")
        command = self.build(nasty)
        video_filter = command[command.index("-vf") + 1]
        self.assertNotIn("a:b,c'd[e]", video_filter)

    def test_banner_files_are_written_and_readable(self):
        Broadcaster(self.config, DEFAULT_CHANNELS[0], self.directory)
        names = os.listdir(self.directory)
        self.assertIn("fake1.bug.txt", names)
        self.assertIn("fake1.now.txt", names)
        with open(os.path.join(self.directory, "fake1.bug.txt"), encoding="utf-8") as handle:
            self.assertIn(DEFAULT_CHANNELS[0].name, handle.read())

    def test_profiled_broadcasters_do_not_share_banner_files(self):
        Broadcaster(self.config, DEFAULT_CHANNELS[0], self.directory)
        Broadcaster(self.config, DEFAULT_CHANNELS[0], self.directory, "mobile")
        names = set(os.listdir(self.directory))
        self.assertIn("fake1.bug.txt", names)
        self.assertIn("fake1-mobile.bug.txt", names)


class SubscriberTests(unittest.TestCase):
    def test_a_slow_client_drops_old_data_instead_of_blocking(self):
        subscriber = Subscriber("ch")
        for index in range(subscriber.queue.maxsize + 20):
            subscriber.offer(bytes([index % 256]))
        self.assertTrue(subscriber.queue.full())
        self.assertEqual(subscriber.dropped, 20)

    def test_close_ends_the_stream(self):
        subscriber = Subscriber("ch")
        subscriber.offer(b"data")
        subscriber.close()
        self.assertEqual(subscriber.read(), b"data")
        self.assertIsNone(subscriber.read())

    def test_read_returns_empty_on_timeout(self):
        self.assertEqual(Subscriber("ch").read(timeout=0.01), b"")


if __name__ == "__main__":
    unittest.main()
