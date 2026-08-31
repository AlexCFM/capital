#!/usr/bin/env python3
"""
now_playing.py — publishes the track currently playing on the CAPITAL FM
Icecast stream as a small JSON file.

It keeps one long-lived ffmpeg process attached to the stream and watches its
log for ICY StreamTitle updates. Icecast only sends a metadata block when the
title actually changes (plus once on connect), so this sits idle almost all of
the time and picks up a track change the moment it happens — no polling, and
one upstream connection no matter how many listeners the site has.

Every change is written to OUTPUT_FILE as JSON, replaced atomically so a web
server can never serve a half-written file:

    {
      "raw":        "Artist - Title",
      "artist":     "Artist",
      "title":      "Title",
      "updated_at": "2026-08-26T18:00:00+00:00",
      "updated_unix": 1787764800
    }

Serve that file over HTTPS with `Access-Control-Allow-Origin: *` and point the
site's METADATA_URL at it. See README.md for an nginx snippet.

Requires: python3 (stdlib only) and ffmpeg on PATH. No third-party packages.

Usage:
    ./now_playing.py --url http://icecast.vgtrk.cdnvideo.ru/capitalfmmp3 \
                     --output /var/www/nowplaying/capital.json

All options can also be set via environment variables — see main() below.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

# ffmpeg reports ICY metadata in one of two shapes depending on version and
# log level. Both are accepted so this keeps working across upgrades:
#   1) the metadata block it prints for the input:  "    StreamTitle     : Foo"
#   2) the raw tag straight from the http protocol: "StreamTitle='Foo';"
RE_QUOTED = re.compile(r"StreamTitle\s*=\s*'(.*?)'\s*;")
RE_COLON = re.compile(r"^\s*StreamTitle\s*:\s*(.*?)\s*$")

# ffmpeg emits a "progress" report on stdout once a second. Nothing is done
# with the values — it is purely a heartbeat, so a connection that goes quiet
# without ffmpeg noticing can be detected and restarted instead of hanging
# forever on a stream that stopped delivering.
PROGRESS_ARGS = ["-progress", "pipe:1"]

_shutdown = threading.Event()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_stream_title(line):
    """Return the StreamTitle value in `line`, or None if there isn't one."""
    m = RE_QUOTED.search(line)
    if m:
        return m.group(1).strip()
    m = RE_COLON.match(line)
    if m:
        return m.group(1).strip()
    return None


def split_artist_title(raw):
    """
    Split "Artist - Title" into its two halves.

    Only the first " - " is treated as the separator, so a title that itself
    contains a dash ("Artist - Song - Remix") keeps the remainder intact. If
    there is no separator at all the whole string is treated as the title and
    the artist is left empty, which is what station idents and ad markers
    ("Commercial") look like.
    """
    parts = raw.split(" - ", 1)
    if len(parts) == 2:
        artist, title = parts[0].strip(), parts[1].strip()
        if artist and title:
            return artist, title
    return "", raw


def write_atomic(path, payload):
    """
    Write JSON to `path` so readers only ever see a complete file.

    The temp file is created in the same directory (os.replace is only atomic
    within a filesystem) and fsync'd before the rename, so a power loss can't
    leave a truncated file behind either.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def publish(path, raw):
    artist, title = split_artist_title(raw)
    now = datetime.now(timezone.utc)
    write_atomic(path, {
        "raw": raw,
        "artist": artist,
        "title": title,
        "updated_at": now.isoformat(timespec="seconds"),
        "updated_unix": int(now.timestamp()),
    })


def build_ffmpeg_command(args):
    cmd = [
        args.ffmpeg,
        "-hide_banner",
        "-nostdin",       # never try to read the terminal; this runs as a service
        "-nostats",       # stats would drown out the metadata lines on stderr
        "-loglevel", "verbose",
        # ffmpeg's own reconnect handling covers the common case of a brief
        # drop, so the process usually survives without a full restart.
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-user_agent", args.user_agent,
        "-i", args.url,
    ]
    cmd += PROGRESS_ARGS
    # Decoding is what the stream was verified against and costs ~1% of a core
    # for 128 kbps MP3. `-c copy` skips even that; enable it if CPU matters and
    # your ffmpeg still reports metadata updates with it (test before relying).
    if args.copy_codec:
        cmd += ["-c", "copy"]
    cmd += ["-f", "null", "-"]
    return cmd


def pump_stderr(proc, state, args):
    """Read ffmpeg's log and publish every *change* of StreamTitle."""
    for line in proc.stderr:
        if _shutdown.is_set():
            return
        raw = parse_stream_title(line)
        if raw is None:
            continue
        if not raw:
            # An empty StreamTitle is Icecast saying "nothing to report";
            # keep showing the last real track rather than blanking the site.
            continue
        if raw == state["last_title"]:
            # ffmpeg re-prints the whole metadata block on every update, so
            # unchanged repeats are normal and must not be republished.
            continue
        state["last_title"] = raw
        publish(args.output, raw)
        log(f"now playing: {raw}")


def pump_stdout(proc, state):
    """Consume the progress heartbeat and record when it last arrived."""
    for _ in proc.stdout:
        if _shutdown.is_set():
            return
        state["last_progress"] = time.monotonic()


def run_once(args, state):
    """
    Run ffmpeg until it exits or stalls. Returns when the process is gone.
    """
    cmd = build_ffmpeg_command(args)
    log(f"starting: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        errors="replace",
    )
    state["last_progress"] = time.monotonic()

    threads = [
        threading.Thread(target=pump_stderr, args=(proc, state, args), daemon=True),
        threading.Thread(target=pump_stdout, args=(proc, state), daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while proc.poll() is None:
            if _shutdown.is_set():
                break
            silent_for = time.monotonic() - state["last_progress"]
            if silent_for > args.stall_timeout:
                log(f"no progress for {silent_for:.0f}s — restarting ffmpeg")
                break
            time.sleep(1)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass

    return proc.returncode


def main():
    p = argparse.ArgumentParser(
        description="Publish the current CAPITAL FM track as JSON.",
    )
    p.add_argument(
        "--url",
        default=os.environ.get("STREAM_URL", "http://icecast.vgtrk.cdnvideo.ru/capitalfmmp3"),
        help="Icecast stream URL (env: STREAM_URL)",
    )
    p.add_argument(
        "--output",
        default=os.environ.get("OUTPUT_FILE", "/var/www/nowplaying/capital.json"),
        help="where to write the JSON (env: OUTPUT_FILE)",
    )
    p.add_argument(
        "--ffmpeg",
        default=os.environ.get("FFMPEG_BIN", "ffmpeg"),
        help="ffmpeg binary (env: FFMPEG_BIN)",
    )
    p.add_argument(
        "--user-agent",
        default=os.environ.get("USER_AGENT", "capital-now-playing/1.0"),
        help="User-Agent sent to Icecast (env: USER_AGENT)",
    )
    p.add_argument(
        "--stall-timeout",
        type=float,
        default=float(os.environ.get("STALL_TIMEOUT", "30")),
        help="restart ffmpeg after this many seconds without progress "
             "(env: STALL_TIMEOUT)",
    )
    p.add_argument(
        "--copy-codec",
        action="store_true",
        default=os.environ.get("COPY_CODEC", "").lower() in ("1", "true", "yes"),
        help="pass -c copy to skip audio decoding (env: COPY_CODEC)",
    )
    args = p.parse_args()

    def handle_signal(signum, _frame):
        log(f"got signal {signum}, shutting down")
        _shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(f"stream:  {args.url}")
    log(f"output:  {args.output}")

    state = {"last_title": None, "last_progress": time.monotonic()}
    backoff = 1.0
    while not _shutdown.is_set():
        started = time.monotonic()
        try:
            code = run_once(args, state)
        except FileNotFoundError:
            log(f"ffmpeg not found at {args.ffmpeg!r} — install it or pass --ffmpeg")
            return 1
        except Exception as e:  # keep the service alive through anything odd
            log(f"unexpected error: {e!r}")
            code = -1

        if _shutdown.is_set():
            break

        # A run that lasted a while was healthy, so don't punish the next
        # attempt with a long delay; only repeated fast failures back off.
        if time.monotonic() - started > 60:
            backoff = 1.0
        log(f"ffmpeg exited (code {code}); reconnecting in {backoff:.0f}s")
        _shutdown.wait(backoff)
        backoff = min(backoff * 2, 60.0)

    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
