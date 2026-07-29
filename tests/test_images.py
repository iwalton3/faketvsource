"""Logo styles and the ffmpeg command built for a still.

The point of the styles is to be hostile to the client, so what these tests
guard is that the lineup keeps at least one of each hostile case and that a
transparent style really does reach ffmpeg as a transparent background — a
missing `format=rgba` or `-pix_fmt rgba` silently flattens it onto black,
which would quietly turn every adversarial logo back into a well-behaved one.
"""

import unittest

from faketv.config import DEFAULT_CHANNELS, LOGO_STYLES, Channel, Config, ConfigError, parse_channel
from faketv.images import ImageFactory, logo_paint, logo_style_for


def channel(**kwargs) -> Channel:
    base = {"id": "c", "number": "1.1", "name": "C", "color": "0x123456"}
    return Channel(**{**base, **kwargs})


class LogoStyleTests(unittest.TestCase):
    def test_solid_keeps_the_channel_colour_and_white_text(self):
        background, text = logo_paint(channel(logo_style="solid"))
        self.assertEqual(background, "0x123456@1")
        self.assertEqual(text, "white")

    def test_transparent_styles_differ_only_in_text_colour(self):
        light = logo_paint(channel(logo_style="light-on-transparent"))
        dark = logo_paint(channel(logo_style="dark-on-transparent"))
        self.assertEqual(light[0], "black@0")
        self.assertEqual(dark[0], "black@0")
        self.assertEqual(light[1], "white")
        self.assertEqual(dark[1], "black")

    def test_translucent_carries_partial_alpha(self):
        background, _ = logo_paint(channel(logo_style="translucent"))
        self.assertEqual(background, "0x123456@0.35")

    def test_an_unknown_style_falls_back_rather_than_failing_a_request(self):
        style = logo_style_for(channel(logo_style="nonsense"))
        self.assertEqual(style, LOGO_STYLES["solid"])

    def test_config_rejects_an_unknown_style(self):
        with self.assertRaises(ConfigError):
            parse_channel({"id": "a", "name": "A", "logo_style": "chartreuse"}, 0)

    def test_the_default_lineup_stays_adversarial(self):
        styles = {c.logo_style for c in DEFAULT_CHANNELS}
        for style in LOGO_STYLES:
            self.assertIn(style, styles, f"no default channel uses {style}")
        transparent = [c for c in DEFAULT_CHANNELS if logo_style_for(c).alpha == 0.0]
        text = {logo_style_for(c).text for c in transparent}
        # Both a white-text and a black-text logo with nothing behind them, so
        # neither a light nor a dark background can render the whole lineup.
        self.assertEqual(text, {"white", "black"})


class LogoCommandTests(unittest.TestCase):
    def setUp(self):
        self.factory = ImageFactory(Config(channels=DEFAULT_CHANNELS, font_file=None))

    def command(self, alpha: bool) -> list[str]:
        return self.factory._command(400, 400, "black@0", ["drawtext=text=x"], alpha)

    def test_a_transparent_background_asks_for_rgba_at_both_ends(self):
        command = self.command(alpha=True)
        source = command[command.index("-i") + 1]
        self.assertTrue(source.endswith(",format=rgba"), source)
        self.assertEqual(command[command.index("-pix_fmt") + 1], "rgba")

    def test_an_opaque_background_stays_rgb(self):
        command = self.command(alpha=False)
        self.assertNotIn("-pix_fmt", command)
        self.assertNotIn("format=rgba", command[command.index("-i") + 1])

    def test_the_still_is_a_single_png_frame_on_stdout(self):
        command = self.command(alpha=True)
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[command.index("-c:v") + 1], "png")
        self.assertEqual(command[-1], "pipe:1")


if __name__ == "__main__":
    unittest.main()
