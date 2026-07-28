"""The guide has to be gapless and stable, or Jellyfin's grid goes strange."""

import unittest
from datetime import date, datetime, timedelta, timezone

from faketv.config import DEFAULT_CHANNELS
from faketv.schedule import MINUTES_PER_DAY, PROFILES, Guide

DAY = date(2026, 3, 14)


class ScheduleShapeTests(unittest.TestCase):
    def setUp(self):
        self.guide = Guide("test-seed")

    def test_every_profile_fills_the_day_exactly(self):
        for name in PROFILES:
            with self.subTest(profile=name):
                programmes = self.guide.day("ch", name, DAY)
                self.assertTrue(programmes)

                midnight = datetime(2026, 3, 14, tzinfo=timezone.utc)
                self.assertEqual(programmes[0].start, midnight)
                self.assertEqual(programmes[-1].stop, midnight + timedelta(days=1))

                total = sum(p.duration.total_seconds() for p in programmes)
                self.assertEqual(total, MINUTES_PER_DAY * 60)

    def test_programmes_are_contiguous(self):
        for name in PROFILES:
            with self.subTest(profile=name):
                programmes = self.guide.day("ch", name, DAY)
                for earlier, later in zip(programmes, programmes[1:]):
                    self.assertEqual(earlier.stop, later.start)

    def test_durations_are_whole_slots(self):
        for name, profile in PROFILES.items():
            with self.subTest(profile=name):
                for programme in self.guide.day("ch", name, DAY):
                    minutes = programme.duration.total_seconds() / 60
                    self.assertEqual(minutes % profile.slot, 0, programme.title)

    def test_days_join_up_across_the_boundary(self):
        today = self.guide.day("ch", "movies", DAY)
        tomorrow = self.guide.day("ch", "movies", DAY + timedelta(days=1))
        self.assertEqual(today[-1].stop, tomorrow[0].start)


class DeterminismTests(unittest.TestCase):
    def test_same_seed_gives_the_same_guide(self):
        # Jellyfin re-fetches the XMLTV file hourly and matches recordings
        # against it, so a restart must not reshuffle the schedule.
        first = Guide("stable").day("ch", "general", DAY)
        second = Guide("stable").day("ch", "general", DAY)
        self.assertEqual(
            [(p.start, p.title, p.episode) for p in first],
            [(p.start, p.title, p.episode) for p in second],
        )

    def test_different_seeds_differ(self):
        one = Guide("alpha").day("ch", "general", DAY)
        two = Guide("beta").day("ch", "general", DAY)
        self.assertNotEqual([p.title for p in one], [p.title for p in two])

    def test_channels_do_not_share_a_schedule(self):
        guide = Guide("stable")
        one = guide.day("ch1", "general", DAY)
        two = guide.day("ch2", "general", DAY)
        self.assertNotEqual([p.title for p in one], [p.title for p in two])


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.guide = Guide("test-seed")

    def test_between_covers_a_programme_still_running_at_the_start(self):
        # A three-hour film that began before the window must still show up.
        window_start = datetime(2026, 3, 14, 12, 20, tzinfo=timezone.utc)
        found = self.guide.between("ch", "movies", window_start, window_start + timedelta(minutes=5))
        self.assertEqual(len(found), 1)
        self.assertLessEqual(found[0].start, window_start)
        self.assertGreater(found[0].stop, window_start)

    def test_something_is_always_on(self):
        start = datetime(2026, 3, 14, tzinfo=timezone.utc)
        for minutes in range(0, MINUTES_PER_DAY, 7):
            when = start + timedelta(minutes=minutes)
            for name in PROFILES:
                current, upcoming = self.guide.now_and_next("ch", name, when)
                self.assertIsNotNone(current, f"{name} at {when}")
                self.assertLessEqual(current.start, when)
                self.assertGreater(current.stop, when)
                self.assertIsNotNone(upcoming)
                self.assertEqual(current.stop, upcoming.start)

    def test_default_channels_all_use_a_known_profile(self):
        for channel in DEFAULT_CHANNELS:
            self.assertIn(channel.profile, PROFILES)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.guide = Guide("test-seed")

    def test_series_profiles_number_their_episodes(self):
        for programme in self.guide.day("ch", "general", DAY):
            self.assertIsNotNone(programme.season)
            self.assertIsNotNone(programme.episode)
            self.assertTrue(programme.is_series)

    def test_movies_carry_a_year_and_a_rating(self):
        for programme in self.guide.day("ch", "movies", DAY):
            self.assertIsNotNone(programme.year)
            self.assertIsNotNone(programme.star_rating)
            self.assertFalse(programme.is_series)

    def test_categories_hit_jellyfins_keywords(self):
        # Jellyfin flags a programme as news/sports/kids/movie by looking for
        # these words in the XMLTV categories.
        wanted = {"movies": "movie", "news": "news", "sports": "sports", "kids": "kids"}
        for name, keyword in wanted.items():
            categories = {c.lower() for c in PROFILES[name].categories}
            self.assertIn(keyword, categories, name)

    def test_a_programme_is_never_both_new_and_a_repeat(self):
        for name in PROFILES:
            for programme in self.guide.day("ch", name, DAY):
                self.assertNotEqual(programme.is_new, programme.previously_shown)
                if programme.is_premiere:
                    self.assertFalse(programme.previously_shown)


if __name__ == "__main__":
    unittest.main()
