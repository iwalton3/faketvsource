# faketvsource

A fake Live TV source for testing Jellyfin clients without a tuner. Serves an
M3U playlist, an XMLTV guide, and MPEG-TS streams of an ffmpeg test pattern
captioned with the channel, the current programme and a clock.

Read `README.md` first — it covers what this is and how to run it. This file is
about working on the code.

## Ground rules

Python standard library plus `ffmpeg`. Nothing else. This is a test tool; a
dependency that can rugpull is worse than a hundred lines of code. If something
seems to need a package, it almost certainly does not.

Run the tests with `python3 -m unittest discover -s tests -t .` (61 tests, under
a second, no ffmpeg). To try it for real: `./faketv.py --port 8409 -v`, then
`curl -s -m 8 http://127.0.0.1:8409/stream/fake2.ts -o /tmp/s.ts` and pull a
frame out with `ffmpeg -ss 5 -i /tmp/s.ts -frames:v 1 /tmp/f.png`. `-v` logs the
full ffmpeg command line, which is the fastest way to debug a filter problem.

## Layout

| File | What lives there |
| --- | --- |
| `faketv/config.py` | frozen dataclasses, JSON loading, defaults, the default lineup |
| `faketv/schedule.py` | `Guide` — deterministic programme generation, and the profile table |
| `faketv/xmltv.py` | XMLTV rendering |
| `faketv/playlist.py` | M3U rendering |
| `faketv/hdhr.py` | HDHomeRun emulation (discover/lineup/device.xml) |
| `faketv/encoder.py` | `Broadcaster` (one ffmpeg per channel) and `StreamManager` |
| `faketv/images.py` | logos and programme art, rendered by ffmpeg and cached |
| `faketv/server.py` | HTTP routing, the landing page |
| `faketv/cli.py` | argument parsing, startup, signal handling |

Jellyfin's own Live TV source is at `../jellyfin/` — `src/Jellyfin.LiveTv/`, in
particular `TunerHosts/M3uParser.cs`, `Listings/XmlTvListingsProvider.cs` and
`TunerHosts/HdHomerun/HdHomerunHost.cs`. When in doubt about what Jellyfin
accepts, read it there rather than guessing.

## Invariants worth not breaking

**The guide must stay deterministic.** Jellyfin caches the XMLTV file and
re-fetches it hourly, then matches recordings against it. Anything that makes
the same `(seed, channel, date)` produce a different schedule turns every
refresh into an apparent listings change and orphans recordings. In practice:
never seed a PRNG from anything but `_rng(...)` in `schedule.py` (Python's
string hash is salted per process, so `random.Random("text")` is *not* stable
across restarts), and never use wall-clock time in generation.

**Programme durations must be whole multiples of the profile's slot, and the
slot must divide 1440.** That is what makes a day's listings land exactly on
midnight, so days join without a gap or an overlap. `test_schedule.py` checks
this for every profile; if you add one, it gets checked automatically.

**M3U `tvg-id` must equal the XMLTV `<channel id>`.** That is the whole reason
guide data attaches to the right channel without manual mapping.

**Categories are keyword-matched by Jellyfin.** `ListingsProviderInfo` defaults
to looking for `movie`, `news`, `sports`, and `kids`/`family`/`children` in the
XMLTV categories to set `IsMovie`/`IsNews`/`IsSports`/`IsKids`. Renaming a
profile's categories silently drops those flags.

**The logos are meant to be hostile.** Four of the six ship with a fully
transparent background, two with white text and two with black, so no single
background colour renders the whole lineup. That is the feature: a client that
composites logos badly is supposed to fail here. Do not "fix" the lineup by
making them all opaque, and do not make the transparent styles legible by
adding a shadow or an outline — that is exactly the accommodation a client
ought to be making for itself. `test_images.py` fails if the lineup stops
covering all four styles or loses either the white- or the black-text
transparent case. The landing page is the one place that composites them onto
a mid-tone checker, because that is what correct handling looks like.

**The HDHomeRun model number must contain `hdtc`.** Jellyfin decides a device
can transcode by substring-matching that, which is what makes it offer the
alternate quality media sources that `?transcode=<profile>` serves.

## ffmpeg gotchas, all learned the hard way

**Text goes through files, never the filter string.** Channel names and
programme titles are written to small files that `drawtext` reads with
`textfile=` and `expansion=none`, replaced atomically with `os.replace`. This
is why a channel called `a:b,c'd[e]` cannot corrupt the filter graph, and why
the now/next caption can change at a programme boundary without restarting the
encoder. There is a test for the hostile-name case; keep it passing.

**Only argument-less time expansions work in the filter string.** `%{localtime}`
and `%{gmtime}` are fine. `%{localtime:%H\:%M}` is not — the escaping that works
through a shell does not work when the filter is passed as an argv element, and
it fails at filter-parse time with a confusing "No option name near" error.

**A transparent background needs `format=rgba` *and* `-pix_fmt rgba`.** The
`color` source picks its pixel format by negotiation, so `color=c=black@0`
alone gets flattened onto opaque black without a word of complaint — and the
resulting logo looks perfectly fine, which is how it goes unnoticed. Both ends
are set in `ImageFactory._command`, and `test_images.py` checks both.

**The muxer options are `-metadata service_name=` / `service_provider=`.** There
is no `-mpegts_service_name`.

**`Popen(bufsize=0)` gives a raw `FileIO`, which has no `read1`.** Use `.read(n)`
— on an unbuffered pipe it is already a single syscall returning what is
available, which is the behaviour we want.

## Threading

`ThreadingHTTPServer` with one thread per request; a stream request occupies its
thread for as long as the client watches. Each `Broadcaster` adds a pump thread
and a stderr-drain thread, and `StreamManager` runs one janitor thread that
refreshes every live channel's now/next caption once a second and retires
encoders that have been idle past `stream_linger_seconds`.

`Broadcaster._lock` guards the subscriber set and the process handle;
`StreamManager._lock` guards the broadcaster dict. Never call `Broadcaster.stop()`
while holding `StreamManager._lock` — it waits on the process.

Slow clients are dropped from, never blocked on: `Subscriber.offer` discards the
oldest chunk when the queue is full, so one stalled viewer cannot back up a
channel everyone else is watching.

## Things deliberately not implemented

- **HDHomeRun UDP discovery** (port 65001). Jellyfin assumes a discovered device
  answers HTTP on port 80, which would mean running this as root. Tuners are
  added by URL instead.
- **Recording.** That is Jellyfin's side of the wire; there is nothing to fake.
