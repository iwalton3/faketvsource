"""ffmpeg process management and stream fan-out.

One encoder runs per channel, no matter how many clients are watching. Besides
being cheaper, it is what makes this behave like real live TV: everyone tuned
to a channel sees the same clock and the same frame at the same moment, and a
client that joins late gets the stream from *now* rather than from the start.
"""

from __future__ import annotations

import logging
import os
import queue
import shlex
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

from .config import Channel, Config
from .schedule import Guide

LOG = logging.getLogger("faketv.encoder")

# Read size for the pump. A multiple of the 188-byte MPEG-TS packet so we never
# hand a client a partial packet at the head of its buffer.
CHUNK = 188 * 350

# Per-client backlog, in chunks. A client that falls further behind than this
# is losing data faster than it can drink it, so we drop rather than stall the
# whole channel.
CLIENT_BACKLOG = 256

# An encoder that exits sooner than this after launching is treated as broken
# rather than unlucky, and counts toward the give-up threshold.
MIN_HEALTHY_RUNTIME = 10.0
MAX_QUICK_FAILURES = 5

# The quality profiles an HDHomeRun with hardware transcoding advertises.
# Jellyfin turns these into alternate media sources on the channel, so honouring
# them is what lets a client test switching between stream qualities.
TRANSCODE_PROFILES: dict[str, tuple[int, int, str]] = {
    "heavy": (1920, 1080, "8000k"),
    "internet720": (1280, 720, "5000k"),
    "mobile": (1280, 720, "2000k"),
    "internet540": (960, 540, "2500k"),
    "internet480": (848, 480, "2000k"),
    "internet360": (640, 360, "1500k"),
    "internet240": (432, 240, "1000k"),
}


class TunerBusy(Exception):
    """Raised when the simulated tuner count is exhausted."""


class Subscriber:
    """One client's view of a channel."""

    def __init__(self, channel_id: str) -> None:
        # The broadcaster key, which is the channel id plus any quality profile.
        self.channel_id = channel_id
        self.queue: queue.Queue[bytes | None] = queue.Queue(maxsize=CLIENT_BACKLOG)
        self.dropped = 0
        self.sent = 0

    def offer(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except queue.Full:
            # Discard the oldest chunk to make room; falling behind should cost
            # this client some frames, not stall every other viewer.
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(data)
            except queue.Full:
                self.dropped += 1

    def close(self) -> None:
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass

    def read(self, timeout: float = 10.0) -> bytes | None:
        """Next chunk, or None when the stream has ended."""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return b""


def escape_filter_value(value: str) -> str:
    """Escape a value for use inside an ffmpeg filter option."""
    for char in ("\\", ":", "'", "[", "]", ",", ";"):
        value = value.replace(char, "\\" + char)
    return value


def stream_key(channel_id: str, profile: str | None) -> str:
    """Identity of a broadcaster: a channel, optionally at a given quality."""
    return f"{channel_id}@{profile}" if profile else channel_id


class Broadcaster:
    """Runs one ffmpeg for a channel and fans its output out to subscribers."""

    def __init__(
        self,
        config: Config,
        channel: Channel,
        banner_dir: str,
        profile: str | None = None,
    ) -> None:
        self.config = config
        self.channel = channel
        self.profile = profile if profile in TRANSCODE_PROFILES else None
        self.key = stream_key(channel.id, self.profile)
        self.started_at = time.monotonic()
        self.idle_since: float | None = time.monotonic()

        self._lock = threading.Lock()
        self._subscribers: set[Subscriber] = set()
        self._process: subprocess.Popen[bytes] | None = None
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._launched_at = 0.0
        self._quick_failures = 0
        self.failed = False

        safe = self.key.replace("@", "-")
        self._bug_file = os.path.join(banner_dir, f"{safe}.bug.txt")
        self._now_file = os.path.join(banner_dir, f"{safe}.now.txt")
        bug = f"{channel.number}  {channel.name}"
        if self.profile:
            # Naming the quality on screen is how you tell which media source a
            # client actually picked.
            bug += f"   [{self.profile}]"
        self._write_banner(self._bug_file, bug)
        self._write_banner(self._now_file, "Now: ---")

    # -- banner files -----------------------------------------------------

    @staticmethod
    def _write_banner(path: str, text: str) -> None:
        """Replace a drawtext source file atomically.

        drawtext re-opens the path periodically; writing in place would let it
        read a half-written file and flash garbage on screen.
        """
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)

    def update_now_next(self, guide: Guide) -> None:
        now = datetime.now(timezone.utc)
        current, upcoming = guide.now_and_next(self.channel.id, self.channel.profile, now)

        lines = []
        if current:
            label = current.title
            if current.season is not None and current.episode is not None:
                label += f"  (S{current.season:02d}E{current.episode:02d})"
            lines.append(f"Now:  {label}")
            lines.append(f"      {current.start:%H:%M} - {current.stop:%H:%M} UTC")
        else:
            lines.append("Now:  ---")
        if upcoming:
            lines.append(f"Next: {upcoming.title}  at {upcoming.start:%H:%M} UTC")

        self._write_banner(self._now_file, "\n".join(lines))

    # -- ffmpeg -----------------------------------------------------------

    def command(self) -> list[str]:
        config = self.config
        video = config.video
        channel = self.channel

        width = channel.width or video.width
        height = channel.height or video.height
        bitrate = video.bitrate
        if self.profile is not None:
            width, height, bitrate = TRANSCODE_PROFILES[self.profile]

        pattern = channel.pattern or video.pattern
        gop = max(1, int(round(video.fps * video.gop_seconds)))

        source = f"{pattern}=size={width}x{height}:rate={video.fps}"

        audio = config.audio
        if audio.mode == "silence":
            audio_source = f"anullsrc=r={audio.sample_rate}:cl=stereo"
        elif audio.mode == "beep":
            audio_source = (
                f"sine=frequency={channel.tone}:beep_factor=4"
                f":sample_rate={audio.sample_rate},volume={audio.volume}"
            )
        else:
            audio_source = (
                f"sine=frequency={channel.tone}:sample_rate={audio.sample_rate}"
                f",volume={audio.volume}"
            )

        # Scale the overlay with the frame so an SD channel is not covered by
        # text sized for 1080p.
        unit = height / 720.0
        big = max(16, int(44 * unit))
        mid = max(13, int(32 * unit))
        pad = max(8, int(36 * unit))
        border = max(4, int(12 * unit))

        common = ["box=1", "boxcolor=black@0.6", f"boxborderw={border}", "fontcolor=white"]
        if config.font_file:
            common.append(f"fontfile={escape_filter_value(config.font_file)}")
        else:
            common.append("font=sans")

        def drawtext(*options: str) -> str:
            return "drawtext=" + ":".join([*common, *options])

        # Stack the bottom-left rows by their distance from the bottom edge.
        # Each drawtext knows its own height as `th`, so multi-line blocks size
        # themselves correctly.
        row_clock = pad
        row_utc = pad + big + 16
        row_now = row_utc + mid + 18

        reload_every = max(1, video.fps)
        filters = [
            # Channel bug, top left. Read from a file so a channel name with
            # colons or quotes in it can never break the filter graph.
            drawtext(
                f"textfile={escape_filter_value(self._bug_file)}",
                "expansion=none",
                f"fontsize={big}",
                f"x={pad}",
                f"y={pad}",
            ),
            # Now/next, refreshed once a second by the janitor thread.
            drawtext(
                f"textfile={escape_filter_value(self._now_file)}",
                "expansion=none",
                f"reload={reload_every}",
                "fontcolor=yellow",
                f"fontsize={mid}",
                f"x={pad}",
                f"y=h-th-{row_now}",
            ),
            # UTC clock, matching the times the guide is written in. Handy for
            # spotting a client that mangles the timezone.
            drawtext(
                "text=UTC %{gmtime}",
                f"fontsize={mid}",
                "fontcolor=0xbbbbbb",
                f"x={pad}",
                f"y=h-th-{row_utc}",
            ),
            # Local wall clock, re-rendered every frame. This is the reference
            # you compare against a real clock to judge stream latency.
            drawtext(
                "text=%{localtime}",
                f"fontsize={big}",
                f"x={pad}",
                f"y=h-th-{row_clock}",
            ),
        ]

        return [
            config.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-loglevel", "warning",
            "-re", "-f", "lavfi", "-i", source,
            "-re", "-f", "lavfi", "-i", audio_source,
            "-map", "0:v", "-map", "1:a",
            "-vf", ",".join(filters),
            "-c:v", "libx264",
            "-preset", video.preset,
            "-tune", "zerolatency",
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", bitrate,
            "-g", str(gop),
            "-keyint_min", str(gop),
            "-sc_threshold", "0",
            "-c:a", "aac",
            "-b:a", audio.bitrate,
            "-ar", str(audio.sample_rate),
            "-ac", str(audio.channels),
            "-f", "mpegts",
            # Names the TS service, so the channel is identifiable from the
            # stream itself and not just from the playlist.
            "-metadata", "service_provider=Fake TV Source",
            "-metadata", f"service_name={channel.name}",
            # Repeat the PAT/PMT often so a client that joins mid-stream can
            # identify the programme without waiting.
            "-mpegts_flags", "+resend_headers",
            "-muxdelay", "0",
            "pipe:1",
        ]

    def start(self) -> None:
        with self._lock:
            if self._process is not None or self._stopping or self.failed:
                return
            command = self.command()
            self._launched_at = time.monotonic()
            LOG.info("starting encoder for %s", self.key)
            LOG.debug("ffmpeg: %s", shlex.join(command))
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=0,
            )
            process = self._process

        self._threads = [
            threading.Thread(target=self._pump, args=(process,), daemon=True),
            threading.Thread(target=self._drain_stderr, args=(process,), daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _pump(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        try:
            while True:
                # stdout is unbuffered, so read() returns whatever one syscall
                # yields rather than blocking until CHUNK bytes have piled up.
                data = process.stdout.read(CHUNK)
                if not data:
                    break
                with self._lock:
                    subscribers = list(self._subscribers)
                for subscriber in subscribers:
                    subscriber.offer(data)
                    subscriber.sent += len(data)
        except (OSError, ValueError):
            pass
        except Exception:
            LOG.exception("pump for %s failed", self.key)
        finally:
            self._on_exit(process)

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        try:
            for line in process.stderr:
                message = line.decode("utf-8", "replace").rstrip()
                if message:
                    LOG.warning("[%s] %s", self.key, message)
        except (OSError, ValueError):
            pass

    def _on_exit(self, process: subprocess.Popen[bytes]) -> None:
        """Handle ffmpeg exiting; restart if anyone is still watching."""
        with self._lock:
            if self._stopping or self._process is not process:
                return
            self._process = None
            watchers = len(self._subscribers)
            # An encoder that dies almost immediately is broken, not unlucky.
            if time.monotonic() - self._launched_at < MIN_HEALTHY_RUNTIME:
                self._quick_failures += 1
            else:
                self._quick_failures = 0
            failures = self._quick_failures

        code = process.poll()
        LOG.warning("encoder for %s exited (code %s)", self.key, code)

        if not watchers:
            return

        if failures >= MAX_QUICK_FAILURES:
            LOG.error(
                "encoder for %s failed %d times in a row; giving up. "
                "Run with -v to see the ffmpeg command and its output.",
                self.key,
                failures,
            )
            with self._lock:
                self.failed = True
                subscribers = list(self._subscribers)
            for subscriber in subscribers:
                subscriber.close()
            return

        # Back off before retrying so a transient failure does not spin.
        time.sleep(1.0)
        with self._lock:
            if self._stopping or not self._subscribers:
                return
        LOG.info("restarting encoder for %s (%d watching)", self.key, watchers)
        self.start()

    # -- subscribers ------------------------------------------------------

    def subscribe(self) -> Subscriber:
        subscriber = Subscriber(self.key)
        with self._lock:
            self._subscribers.add(subscriber)
            self.idle_since = None
        self.start()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)
            if not self._subscribers:
                self.idle_since = time.monotonic()
        subscriber.close()

    @property
    def viewers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            process = self._process
            self._process = None
            subscribers = list(self._subscribers)
            self._subscribers.clear()

        for subscriber in subscribers:
            subscriber.close()

        if process is not None:
            LOG.info("stopping encoder for %s", self.key)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass


class StreamManager:
    """Owns every channel's broadcaster and enforces the tuner limit."""

    def __init__(self, config: Config, guide: Guide) -> None:
        self.config = config
        self.guide = guide
        self._lock = threading.Lock()
        self._broadcasters: dict[str, Broadcaster] = {}
        self._banner_dir = tempfile.mkdtemp(prefix="faketv-")
        self._shutdown = threading.Event()
        self._janitor = threading.Thread(target=self._run_janitor, daemon=True)
        self._janitor.start()

    def open(self, channel: Channel, profile: str | None = None) -> Subscriber:
        """Subscribe to a channel, starting its encoder if needed."""
        key = stream_key(channel.id, profile if profile in TRANSCODE_PROFILES else None)
        with self._lock:
            broadcaster = self._broadcasters.get(key)
            if broadcaster is None:
                self._enforce_tuner_limit_locked(key)
                broadcaster = Broadcaster(self.config, channel, self._banner_dir, profile)
                broadcaster.update_now_next(self.guide)
                self._broadcasters[key] = broadcaster
        return broadcaster.subscribe()

    def _enforce_tuner_limit_locked(self, wanted: str) -> None:
        limit = self.config.tuner_count
        if limit <= 0:
            return

        # A channel nobody is watching is only alive because of the linger
        # window; reclaim those before refusing the request.
        for key, broadcaster in list(self._broadcasters.items()):
            if len(self._broadcasters) < limit:
                break
            if broadcaster.viewers == 0:
                del self._broadcasters[key]
                threading.Thread(target=broadcaster.stop, daemon=True).start()

        if len(self._broadcasters) >= limit:
            busy = ", ".join(sorted(self._broadcasters))
            raise TunerBusy(
                f"all {limit} simulated tuners are in use ({busy}); "
                f"cannot also tune {wanted}"
            )

    def close(self, subscriber: Subscriber) -> None:
        with self._lock:
            broadcaster = self._broadcasters.get(subscriber.channel_id)
        if broadcaster is not None:
            broadcaster.unsubscribe(subscriber)

    def status(self) -> list[dict[str, object]]:
        with self._lock:
            broadcasters = list(self._broadcasters.values())
        return [
            {
                "stream": broadcaster.key,
                "channel": broadcaster.channel.id,
                "name": broadcaster.channel.name,
                "profile": broadcaster.profile or "native",
                "viewers": broadcaster.viewers,
                "running": broadcaster.running,
                "uptime_seconds": round(time.monotonic() - broadcaster.started_at, 1),
            }
            for broadcaster in broadcasters
        ]

    def _run_janitor(self) -> None:
        """Refresh now/next banners and retire idle encoders."""
        while not self._shutdown.wait(1.0):
            now = time.monotonic()
            expired: list[Broadcaster] = []
            with self._lock:
                for channel_id, broadcaster in list(self._broadcasters.items()):
                    idle_since = broadcaster.idle_since
                    if idle_since is not None and now - idle_since > self.config.stream_linger_seconds:
                        del self._broadcasters[channel_id]
                        expired.append(broadcaster)
                alive = list(self._broadcasters.values())

            for broadcaster in expired:
                broadcaster.stop()
            for broadcaster in alive:
                try:
                    broadcaster.update_now_next(self.guide)
                except OSError as exc:
                    LOG.warning("could not update banner for %s: %s", broadcaster.key, exc)

    def shutdown(self) -> None:
        self._shutdown.set()
        with self._lock:
            broadcasters = list(self._broadcasters.values())
            self._broadcasters.clear()
        for broadcaster in broadcasters:
            broadcaster.stop()
        for name in os.listdir(self._banner_dir):
            try:
                os.unlink(os.path.join(self._banner_dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self._banner_dir)
        except OSError:
            pass
