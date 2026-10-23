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
      "updated_unix": 1787764800,
      "now":     { ...track... },
      "history": [ { ...track... }, ... ]   # newest first, current excluded
    }

A track is {raw, artist, title, started_at, started_unix, artwork, itunes_url,
preview_url}. Artwork and links come from the iTunes Search API, looked up once
here instead of by every visitor; they are null until the lookup finishes (or
if iTunes has no match). Titles without an "Artist - " part (station idents,
ad markers) and "SHORTS" rubrics are shown as "now" but never enter the
history.

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
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ffmpeg reports ICY metadata in several shapes depending on version and
# log level. All are accepted so this keeps working across upgrades:
#   1) the http protocol's verbose log on every change — the only one that
#      shows up mid-stream:          "[http @ 0x..] Metadata update for StreamTitle: Foo"
#   2) the metadata block it prints for the input, on connect only:
#                                    "    StreamTitle     : Foo"
#   3) the raw tag straight from the http protocol: "StreamTitle='Foo';"
RE_UPDATE = re.compile(r"Metadata update for StreamTitle:\s*(.*?)\s*$")
RE_QUOTED = re.compile(r"StreamTitle\s*=\s*'(.*?)'\s*;")
RE_COLON = re.compile(r"^\s*StreamTitle\s*:\s*(.*?)\s*$")

# ffmpeg emits a "progress" report on stdout once a second. Nothing is done
# with the values — it is purely a heartbeat, so a connection that goes quiet
# without ffmpeg noticing can be detected and restarted instead of hanging
# forever on a stream that stopped delivering.
PROGRESS_ARGS = ["-progress", "pipe:1"]

# Station segments ("SHORTS" rubrics) come through as normal titles; they are
# shown as "now" but kept out of the played-tracks list, which is music only.
NOT_MUSIC_RE = re.compile(r"\bshorts\b", re.IGNORECASE)

ITUNES_SEARCH = "https://itunes.apple.com/search?media=music&entity=song&limit=1&term="

_shutdown = threading.Event()
_lock = threading.Lock()  # guards state["now"] / state["history"] and the file write


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_stream_title(line):
    """Return the StreamTitle value in `line`, or None if there isn't one."""
    m = RE_UPDATE.search(line)
    if m:
        return m.group(1).strip()
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


def itunes_search(term, timeout=10):
    req = urllib.request.Request(
        ITUNES_SEARCH + urllib.parse.quote(term),
        headers={"User-Agent": "capital-now-playing/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        results = json.load(r).get("results") or []
    return results[0] if results else None


def lookup_track(artist, title):
    """
    Return {artwork, itunes_url, preview_url} for a track, or None.

    Station edit/version tags like "(CFM Edit)" aren't in iTunes' catalog and
    make the exact search miss, so a miss is retried with parentheticals
    stripped — same fallback the site used when it searched client-side.
    """
    term = f"{artist} {title}"
    stripped = re.sub(r"\s*\([^)]*\)\s*", " ", term)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    for q in dict.fromkeys([term, stripped]):
        hit = itunes_search(q)
        if hit:
            art = hit.get("artworkUrl100")
            return {
                "artwork": art.replace("100x100bb", "600x600bb") if art else None,
                "itunes_url": hit.get("trackViewUrl"),
                "preview_url": hit.get("previewUrl"),
            }
    return None


def is_music(track):
    return bool(track["artist"]) and not NOT_MUSIC_RE.search(track["raw"])


def make_track(raw):
    artist, title = split_artist_title(raw)
    now = datetime.now(timezone.utc)
    return {
        "raw": raw,
        "artist": artist,
        "title": title,
        "started_at": now.isoformat(timespec="seconds"),
        "started_unix": int(now.timestamp()),
        "artwork": None,
        "itunes_url": None,
        "preview_url": None,
    }


def write_state(path, state):
    """Write the current state; caller holds _lock."""
    cur = state["now"]
    write_atomic(path, {
        # flat fields kept so anything reading the old format keeps working
        "raw": cur["raw"],
        "artist": cur["artist"],
        "title": cur["title"],
        "updated_at": cur["started_at"],
        "updated_unix": cur["started_unix"],
        "now": cur,
        "history": state["history"],
    })


def load_state(path, state):
    """Pick the history back up after a restart so the list isn't wiped."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    state["history"] = [t for t in data.get("history") or [] if isinstance(t, dict)]
    now = data.get("now")
    if isinstance(now, dict) and now.get("raw"):
        state["now"] = now
        state["last_title"] = now["raw"]


def enrich(path, state, track):
    """Look the track up on iTunes in the background and republish."""
    try:
        info = lookup_track(track["artist"], track["title"])
    except Exception as e:
        log(f"itunes lookup failed for {track['raw']!r}: {e!r}")
        return
    if not info:
        log(f"no itunes match for {track['raw']!r}")
        return
    with _lock:
        # the dict may have moved into history by now — updating it in place
        # covers both cases, as long as it is still part of the state at all
        track.update(info)
        if track is state["now"] or any(t is track for t in state["history"]):
            write_state(path, state)


def publish(path, raw, state, history_size):
    track = make_track(raw)
    with _lock:
        prev = state["now"]
        if prev and is_music(prev):
            state["history"] = ([prev] + state["history"])[:history_size]
        state["now"] = track
        write_state(path, state)
    if track["artist"]:
        threading.Thread(target=enrich, args=(path, state, track), daemon=True).start()


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
        publish(args.output, raw, state, args.history_size)
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
    p.add_argument(
        "--history-size",
        type=int,
        default=int(os.environ.get("HISTORY_SIZE", "9")),
        help="how many previous tracks to keep (env: HISTORY_SIZE)",
    )
    args = p.parse_args()

    def handle_signal(signum, _frame):
        log(f"got signal {signum}, shutting down")
        _shutdown.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log(f"stream:  {args.url}")
    log(f"output:  {args.output}")

    state = {"last_title": None, "last_progress": time.monotonic(), "now": None, "history": []}
    load_state(args.output, state)
    if state["history"]:
        log(f"restored {len(state['history'])} history entries")
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
