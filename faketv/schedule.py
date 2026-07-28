"""Deterministic fake programme guide.

The whole schedule is a pure function of (seed, channel id, UTC date), so the
guide a client fetched an hour ago still describes the same programmes now.
That matters because Jellyfin caches the XMLTV file and re-fetches it hourly:
a randomly regenerated guide would make every refresh look like a schedule
change and leave recordings pointing at programmes that no longer exist.

Every profile's durations are whole multiples of its slot length, and 1440
minutes divides evenly by every slot length used, so a day's programmes always
land exactly on midnight and never straddle the boundary into the next day.
"""

from __future__ import annotations

import hashlib
import random
import threading
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Programme:
    """One entry in the fake guide."""

    channel_id: str
    start: datetime
    stop: datetime
    title: str
    desc: str
    categories: tuple[str, ...]
    sub_title: str | None = None
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    rating: str | None = None
    # Out of ten; rendered as "n/10" in XMLTV.
    star_rating: int | None = None
    is_new: bool = False
    is_live: bool = False
    is_premiere: bool = False
    previously_shown: bool = False

    @property
    def duration(self) -> timedelta:
        return self.stop - self.start

    @property
    def is_series(self) -> bool:
        return self.episode is not None

    @property
    def program_id(self) -> str:
        stamp = self.start.strftime("%Y%m%d%H%M")
        return f"{self.channel_id}.{stamp}"


@dataclass(frozen=True)
class Profile:
    """The shape of one channel's day."""

    slot: int
    # (minutes, weight) pairs; minutes must be a multiple of `slot`.
    durations: tuple[tuple[int, int], ...]
    categories: tuple[str, ...]
    titles: tuple[str, ...]
    # Programmes get season/episode numbers and episode titles.
    series: bool = True
    # Programmes carry a production year and a star rating.
    movie: bool = False
    live_chance: float = 0.0
    repeat_chance: float = 0.35
    ratings: tuple[str, ...] = ("TV-G", "TV-PG", "TV-14")
    episode_titles: tuple[str, ...] = (
        "Pilot",
        "The One With The Test Pattern",
        "Colour Bars Forever",
        "Signal Lost",
        "A Brief History of Static",
        "Vertical Hold",
        "The Long Fade",
        "Tuned Out",
        "Interference",
        "Please Stand By",
        "Off Air",
        "The Late Feed",
    )


PROFILES: dict[str, Profile] = {
    "general": Profile(
        slot=30,
        durations=((30, 5), (60, 4), (90, 1), (120, 1)),
        categories=("Series", "Entertainment"),
        titles=(
            "The Morning Loop",
            "Placeholder Place",
            "Two Guys and a Waveform",
            "Chromakey Cove",
            "The Bandwidth Bunch",
            "Adventures in Buffering",
            "Latency Lane",
            "Dropped Frames",
            "The Aspect Ratio Show",
            "Everybody Loves Timecode",
        ),
    ),
    "news": Profile(
        slot=30,
        durations=((30, 6), (60, 2)),
        categories=("News", "Current Affairs"),
        titles=(
            "Fake News at the Top of the Hour",
            "The Test Pattern Report",
            "Broadcast Standards Weekly",
            "Nothing Happened Today",
            "The Bulletin That Isn't",
            "Frame Rate Forum",
        ),
        series=False,
        live_chance=0.8,
        repeat_chance=0.05,
        ratings=("TV-G",),
    ),
    "movies": Profile(
        slot=30,
        durations=((90, 3), (120, 4), (150, 1)),
        categories=("Movie", "Feature Film"),
        titles=(
            "Attack of the 50 Foot Colour Bar",
            "The Codec Identity",
            "Citizen Buffer",
            "Gone With the Signal",
            "The Empire Strikes Back Off Air",
            "Raiders of the Lost Frame",
            "2001: A Test Pattern",
            "Interlaced",
            "No Country for Old Formats",
            "The Sound of Sine",
        ),
        series=False,
        movie=True,
        repeat_chance=0.5,
        ratings=("G", "PG", "PG-13", "R"),
    ),
    "sports": Profile(
        slot=30,
        durations=((60, 2), (120, 4), (180, 2)),
        categories=("Sports",),
        titles=(
            "Fake League Football",
            "Competitive Buffering Championship",
            "The Basketball Simulation",
            "Baseball, Probably",
            "Extreme Frame Dropping",
            "The Sunday Signal",
            "Highlights of Nothing",
        ),
        series=False,
        live_chance=0.7,
        repeat_chance=0.15,
        ratings=("TV-G", "TV-PG"),
    ),
    "kids": Profile(
        slot=15,
        durations=((15, 6), (30, 4), (60, 1)),
        categories=("Kids", "Family", "Animation"),
        titles=(
            "Testy the Test Card",
            "Pixel Pals",
            "The Buffering Bunnies",
            "Captain Chroma",
            "Sine Wave Sing-Along",
            "Little House on the Playlist",
            "Adventure Timecode",
        ),
        repeat_chance=0.6,
        ratings=("TV-Y", "TV-Y7", "TV-G"),
    ),
    "retro": Profile(
        slot=30,
        durations=((30, 7), (60, 2)),
        categories=("Series", "Classic"),
        titles=(
            "I Love Latency",
            "The Cathode Ray Hour",
            "Bewitched by Bitrate",
            "The Vertical Blanking Interval",
            "Gunsmoke Signal",
            "The Analogue Zone",
        ),
        repeat_chance=0.9,
        ratings=("TV-G", "TV-PG"),
    ),
}

DEFAULT_PROFILE = "general"

_FLAVOUR = (
    "A fake programme generated for Live TV testing.",
    "There is no actual content here, only a test pattern and a clock.",
    "Synthetic listing data. Any resemblance to real television is accidental.",
    "This entry exists so a guide grid has something to draw.",
    "Placeholder programming, broadcast in glorious test pattern.",
)


def profile_for(name: str) -> Profile:
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])


def _rng(*parts: object) -> random.Random:
    """A Random seeded reproducibly from `parts`.

    Python's string hash is salted per process, so seeding straight from a
    string would give a different schedule on every restart.
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:16], "big"))


def _weighted(rng: random.Random, choices: tuple[tuple[int, int], ...], limit: int) -> int:
    """Pick a duration no longer than `limit`, falling back to `limit` itself."""
    usable = [(value, weight) for value, weight in choices if value <= limit]
    if not usable:
        return limit
    total = sum(weight for _, weight in usable)
    pick = rng.randrange(total)
    for value, weight in usable:
        pick -= weight
        if pick < 0:
            return value
    return usable[-1][0]


class Guide:
    """Generates and caches per-channel daily schedules."""

    def __init__(self, seed: str) -> None:
        self._seed = seed
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, Date], tuple[Programme, ...]] = {}

    def day(self, channel_id: str, profile_name: str, day: Date) -> tuple[Programme, ...]:
        """Return every programme starting on `day` (UTC) for one channel."""
        key = (channel_id, profile_name, day)
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        built = self._build_day(channel_id, profile_name, day)

        with self._lock:
            # A rolling guide only ever touches a handful of days; this cap is
            # a safety net against a client asking for arbitrary dates.
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[key] = built
        return built

    def _build_day(self, channel_id: str, profile_name: str, day: Date) -> tuple[Programme, ...]:
        profile = profile_for(profile_name)
        rng = _rng(self._seed, channel_id, day.isoformat())
        midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        ordinal = day.toordinal()

        programmes: list[Programme] = []
        # Rotate the title pool by day so consecutive days do not look identical.
        titles = list(profile.titles)
        rng.shuffle(titles)

        minute = 0
        index = 0
        while minute < MINUTES_PER_DAY:
            remaining = MINUTES_PER_DAY - minute
            length = _weighted(rng, profile.durations, remaining)
            # Keep the tail of the day on the slot grid so the final programme
            # ends exactly at midnight.
            if remaining - length < profile.slot and remaining != length:
                length = remaining

            title = titles[index % len(titles)]
            start = midnight + timedelta(minutes=minute)
            stop = start + timedelta(minutes=length)

            programmes.append(
                self._make(profile, channel_id, title, start, stop, ordinal, index, rng)
            )

            minute += length
            index += 1

        return tuple(programmes)

    def _make(
        self,
        profile: Profile,
        channel_id: str,
        title: str,
        start: datetime,
        stop: datetime,
        ordinal: int,
        index: int,
        rng: random.Random,
    ) -> Programme:
        # Per-programme RNG so a programme's details do not shift when an
        # earlier programme in the day changes length.
        prng = _rng(self._seed, channel_id, title, start.isoformat())

        season = episode = None
        sub_title = None
        if profile.series:
            offset = int(hashlib.sha256(title.encode("utf-8")).hexdigest()[:4], 16)
            season = 1 + ((ordinal + offset) // 30) % 8
            episode = 1 + (ordinal + offset + index) % 24
            sub_title = profile.episode_titles[(ordinal + offset + index) % len(profile.episode_titles)]

        year = None
        star_rating = None
        if profile.movie:
            year = 1950 + (int(hashlib.sha256(title.encode("utf-8")).hexdigest()[4:8], 16) % 76)
            star_rating = prng.randint(3, 10)

        repeat = prng.random() < profile.repeat_chance
        live = prng.random() < profile.live_chance
        # A premiere is a specific kind of "new", so never claim both a repeat
        # and a premiere for the same programme.
        premiere = not repeat and prng.random() < 0.08

        local_window = f"{start:%H:%M}-{stop:%H:%M} UTC"
        desc = (
            f"{prng.choice(_FLAVOUR)} "
            f"Scheduled {start:%a %d %b} {local_window} on channel {channel_id}."
        )
        if sub_title:
            desc = f"{sub_title}. {desc}"

        return Programme(
            channel_id=channel_id,
            start=start,
            stop=stop,
            title=title,
            sub_title=sub_title,
            desc=desc,
            categories=profile.categories,
            season=season,
            episode=episode,
            year=year,
            rating=prng.choice(profile.ratings) if profile.ratings else None,
            star_rating=star_rating,
            is_new=not repeat,
            is_live=live,
            is_premiere=premiere,
            previously_shown=repeat,
        )

    def between(
        self,
        channel_id: str,
        profile_name: str,
        start: datetime,
        end: datetime,
    ) -> list[Programme]:
        """Every programme overlapping [start, end), in order."""
        found: list[Programme] = []
        # Step back a day so a programme that started yesterday and is still
        # running at `start` is included.
        day = (start - timedelta(days=1)).date()
        last = end.date()
        while day <= last:
            for programme in self.day(channel_id, profile_name, day):
                if programme.stop > start and programme.start < end:
                    found.append(programme)
            day += timedelta(days=1)
        return found

    def now_and_next(
        self,
        channel_id: str,
        profile_name: str,
        when: datetime,
    ) -> tuple[Programme | None, Programme | None]:
        """The programme airing at `when`, and the one after it."""
        window = self.between(channel_id, profile_name, when, when + timedelta(hours=6))
        current = window[0] if window else None
        upcoming = window[1] if len(window) > 1 else None
        return current, upcoming
