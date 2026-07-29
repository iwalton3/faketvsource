# faketvsource

A fake Live TV source for testing Jellyfin clients, so you can exercise Live TV
features without buying a tuner.

It serves an M3U playlist, an XMLTV guide, and one MPEG-TS stream per channel.
Each stream is an ffmpeg test pattern with the channel name, the programme
that's supposedly airing, and a running clock burned into the picture — so you
can tell at a glance which channel a client actually tuned to, and how far
behind live it is.

```
┌─────────────────────────────────────┐
│  2.1  Fake News 24                  │
│                                     │
│        [ ffmpeg test pattern ]      │
│                                     │
│  Now:  Broadcast Standards Weekly   │
│        20:00 - 20:30 UTC            │
│  Next: The Bulletin That Isn't      │
│  UTC 2026-07-28 20:04:05            │
│  2026-07-28 16:04:05                │
└─────────────────────────────────────┘
```

Requirements: Python 3.10+ and `ffmpeg` (with `libx264` and `libfreetype`).
No Python packages to install — it's all standard library.

## Running it

```sh
./faketv.py                       # binds 0.0.0.0:8409
./faketv.py --port 9000
./faketv.py --config myconfig.json
```

It prints the URLs to paste into Jellyfin on startup:

```
faketvsource 1.0.0 serving 6 channels
  web ui / HDHomeRun device  http://192.168.1.20:8409
  M3U playlist               http://192.168.1.20:8409/playlist.m3u
  XMLTV guide                http://192.168.1.20:8409/guide.xml
```

Open the base URL in a browser for a page listing every channel with what's on
now and next.

## Setting up Jellyfin

In **Dashboard → Live TV**, either as an M3U tuner or as a fake HDHomeRun —
they're separate code paths in Jellyfin, so both are worth testing.

**M3U tuner**

1. Tuner Devices → **+** → type **M3U Tuner**
2. File or URL: `http://<host>:8409/playlist.m3u`
3. Save.

**HDHomeRun tuner**

1. Tuner Devices → **+** → type **HD HomeRun**
2. File or URL: `http://<host>:8409`
3. Save.

Jellyfin's HDHomeRun auto-discovery is *not* emulated: it broadcasts on UDP
65001 and then assumes the device answers HTTP on port 80, which would mean
running this as root. Add the tuner by URL instead.

**Guide data** — TV Guide Data Providers → **+** → **XMLTV**, file or URL
`http://<host>:8409/guide.xml`. The playlist's `tvg-id` values match the XMLTV
channel ids, so channels and listings line up with no manual mapping.

Then run **Refresh Guide Data** in Scheduled Tasks and the guide fills in.

## What it gives you to test against

The default lineup is deliberately mixed, so clients get pushed through
different paths:

| No. | Channel | Content | Video | Logo |
| --- | --- | --- | --- | --- |
| 1.1 | Fake One HD | mixed series | 1280×720 testsrc2 | white on transparent |
| 2.1 | Fake News 24 | news, mostly live | 1920×1080 SMPTE HD bars | black on transparent |
| 3.1 | Fake Movies | films with years and star ratings | 1920×1080 testsrc2 | 35% translucent |
| 4.1 | Fake Sports | long live events | 1280×720 testsrc | black on transparent |
| 5.1 | Fake Kids | short episodes | 854×480 testsrc2 | opaque (the control) |
| 6.1 | Fake Retro | repeats | 640×480 SMPTE bars | white on transparent |

Guide entries carry the metadata Jellyfin actually looks at: categories that
trip its news/sports/kids/movie flags, season and episode numbers, episode
titles, content ratings, star ratings, production years, and the
`new`/`premiere`/`live`/`previously-shown` markers. Channel logos and programme
artwork are generated on demand, so image handling gets exercised too.

Each channel has its own audio tone, so you can hear which one is playing.

A few things worth deliberately breaking:

- **Logos that fight back.** Real broadcaster logos are alpha PNGs with no idea
  what they will be drawn on, and a fair number of them are a flat white or
  flat black wordmark with nothing behind it. So most of these are too: four of
  the six are fully transparent, two of those white and two black. A client
  that composites them onto its theme colour, or that assumes a logo brings its
  own background, shows an invisible logo in one theme and a fine one in the
  other — light mode loses 1.1 and 6.1, dark mode loses 2.1 and 4.1. 5.1 is
  opaque as a control, and 3.1 is 35% translucent, which catches clients that
  treat alpha as a 1-bit mask or flatten onto black before scaling. Set every
  channel's `logo_style` to `solid` if you want the polite version back.
- **Tuner exhaustion.** Set `tuner_count` to 1 or 2 and tune more channels than
  that; the server answers `503` exactly like a tuner that's out of capacity.
- **Quality switching.** The fake HDHomeRun claims a transcoding-capable model,
  so Jellyfin offers alternate quality streams. The chosen profile is drawn on
  screen, so you can see which media source a client picked.
- **Timezones.** Both UTC and local time are burned into the picture, and the
  guide is written with explicit UTC offsets.
- **Latency.** The clock updates every frame; compare it with a real one.

## Configuration

Copy `config.example.json` and edit. Every key is optional; anything you leave
out keeps its default, and keys starting with `_` are ignored so you can leave
yourself comments.

| Key | Default | Meaning |
| --- | --- | --- |
| `host`, `port` | `0.0.0.0`, `8409` | bind address |
| `public_url` | *(from the Host header)* | set when Jellyfin can't reach you at the address it asked for — behind a proxy, or in Docker |
| `seed` | `faketv` | schedule seed; same seed, same guide |
| `guide_days` | `5` | how far ahead the guide runs |
| `guide_past_hours` | `12` | how far back it runs |
| `tuner_count` | `0` | max channels streaming at once, `0` for unlimited |
| `stream_linger_seconds` | `15` | how long an encoder stays up after its last viewer |
| `program_images` | `true` | generate programme artwork |
| `ffmpeg` | `ffmpeg` | path to the binary |
| `font_file` | *(autodetected)* | overlay font |
| `video.*` | 1280×720, 30fps, 3000k | defaults for every channel |
| `audio.*` | 128k AAC, `mode: tone` | `tone`, `beep` or `silence` |
| `channels[]` | six fake channels | see below |

A channel takes `id`, `number`, `name`, `group`, `profile`, `color`,
`logo_style`, `tone`, and optional `width`/`height`/`pattern` overrides.
`profile` picks the kind of schedule generated for it: `general`, `news`,
`movies`, `sports`, `kids` or `retro`. `logo_style` picks how the logo is
painted: `solid`, `light-on-transparent`, `dark-on-transparent` or
`translucent`. `pattern` is any lavfi video source — `testsrc`, `testsrc2`,
`smptebars`, `smptehdbars`, `rgbtestsrc`, `color=c=blue`.

The `id` is what ties a channel to its guide listings and to its stream URL, so
changing it re-imports the channel as a new one in Jellyfin.

## Endpoints

| Path | What it is |
| --- | --- |
| `/` | channel list with now/next |
| `/playlist.m3u` | M3U playlist |
| `/guide.xml` | XMLTV guide |
| `/stream/<id>.ts` | MPEG-TS stream (`?transcode=<profile>` for quality) |
| `/logo/<id>.png` | channel logo |
| `/discover.json`, `/lineup.json`, `/lineup_status.json`, `/device.xml` | HDHomeRun emulation |
| `/status.json` | what's currently streaming |
| `/now.json` | what's on every channel right now |

`--print-playlist` and `--print-guide` write those two to stdout and exit, which
is handy for diffing the guide against what Jellyfin ended up importing.

## How it works

**The guide is a pure function of (seed, channel, UTC date).** Jellyfin caches
the XMLTV file and re-fetches it hourly; if the schedule were random, every
refresh would look like the broadcaster changed the listings, and recordings
would point at programmes that no longer exist. Restarting the server, or
running two copies with the same seed, gives you the same guide.

Programme durations are whole multiples of each profile's slot length, and 1440
minutes divides evenly by all of them, so a day's listings always land exactly
on midnight with no gaps or overlaps.

**One encoder per channel, shared by every viewer.** Besides being cheaper, it's
what makes this behave like live TV: everyone watching a channel sees the same
frame at the same moment, and a client that joins late gets the stream from now,
not from the beginning. Short GOPs and repeated PAT/PMT let a late joiner start
decoding quickly. When the last viewer leaves, the encoder stays up for
`stream_linger_seconds` so a client that probes and then plays doesn't pay for
two starts.

**Text reaches ffmpeg through files, not the filter string.** Channel names and
programme titles go into small text files that `drawtext` re-reads once a
second, written with an atomic rename. That's how the now/next caption changes
at a programme boundary without restarting the encoder, and it means a channel
name containing `:` or `'` can't corrupt the filter graph.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

They cover the parts that are easy to get subtly wrong — schedule contiguity and
determinism, the XMLTV and M3U shapes Jellyfin parses, config handling, and
ffmpeg command construction. They don't run ffmpeg.
