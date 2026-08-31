# CAPITAL FM — now-playing metadata publisher

Publishes the track currently playing on the CAPITAL FM Icecast stream as a
small JSON file that the website reads.

## Why a daemon

Browsers cannot read ICY `StreamTitle` metadata from the stream directly: doing
so needs an `Icy-MetaData: 1` request header, which triggers a CORS preflight
that the CDN rejects. So something server-side has to read it and re-publish it.

Icecast sends a metadata block **only when the title actually changes** (plus
once on connect). One long-lived connection therefore costs almost nothing in
CPU and reports a track change the instant it happens — no polling, and a single
upstream connection regardless of how many people have the site open.

The one real cost is bandwidth: reading the metadata means pulling the audio
stream continuously. At 128 kbps that is **~1.4 GB/day (~41 GB/month)**.

## Requirements

- Python 3.7+ (standard library only — no pip packages)
- `ffmpeg` on `PATH`

## Install

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin nowplaying
sudo install -d -o nowplaying -g nowplaying /var/www/nowplaying
sudo install -d /opt/nowplaying
sudo install -m 755 now_playing.py /opt/nowplaying/now_playing.py

sudo cp now-playing.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now now-playing
```

Check it:

```sh
journalctl -u now-playing -f
cat /var/www/nowplaying/capital.json
```

Expect a line like `now playing: Madonna - Read My Lips (CFM Edit)` within a
second or two of starting, and again on every track change.

## Output

`/var/www/nowplaying/capital.json`, replaced atomically on every change so a
web server can never serve a half-written file:

```json
{
  "raw": "Madonna - Read My Lips (CFM Edit)",
  "artist": "Madonna",
  "title": "Read My Lips (CFM Edit)",
  "updated_at": "2026-08-26T18:00:00+00:00",
  "updated_unix": 1787764800
}
```

Notes on the fields:

- `raw` is exactly what the stream sent — use this if you want no guessing.
- `artist`/`title` are split on the **first** `" - "`. When there is no
  separator (station idents, ad markers such as `Commercial`) `artist` is empty
  and the whole string lands in `title`.
- An empty `StreamTitle` is ignored rather than published, so the site keeps
  showing the last real track instead of blanking out.

## Serving it

The website is on HTTPS, so this must be served over HTTPS too (otherwise the
browser blocks it as mixed content) and needs CORS. nginx:

```nginx
location = /capital.json {
    alias /var/www/nowplaying/capital.json;
    default_type application/json;
    add_header Access-Control-Allow-Origin "*" always;
    # The file changes only on a track change; a few seconds of caching keeps
    # bursts of listeners off the disk without making the title visibly stale.
    add_header Cache-Control "public, max-age=5" always;
}
```

Then point `METADATA_URL` in the site's `index.html` at that URL.

Note the site currently expects **plain text** (the track title as the whole
response body), not JSON — see the `pollMetadata()` function. Either adapt that
function to parse JSON, or serve `raw` as text from a second location.

## Configuration

CLI flags, or environment variables of the same name:

| Flag | Env | Default |
| --- | --- | --- |
| `--url` | `STREAM_URL` | `http://icecast.vgtrk.cdnvideo.ru/capitalfmmp3` |
| `--output` | `OUTPUT_FILE` | `/var/www/nowplaying/capital.json` |
| `--ffmpeg` | `FFMPEG_BIN` | `ffmpeg` |
| `--user-agent` | `USER_AGENT` | `capital-now-playing/1.0` |
| `--stall-timeout` | `STALL_TIMEOUT` | `30` |
| `--copy-codec` | `COPY_CODEC` | off |

`--copy-codec` adds `-c copy` so ffmpeg skips audio decoding. Decoding a
128 kbps MP3 costs roughly 1% of a core, so this is only worth enabling if CPU
is tight — and it should be tested first, since metadata reporting under
`-c copy` has not been verified against this stream.

## Reliability

- ffmpeg's own `-reconnect` flags handle brief network drops without a restart.
- If ffmpeg exits, the script restarts it with exponential backoff (1s → 60s).
  A run that stayed up over a minute resets the backoff.
- If the stream stops delivering data *without* ffmpeg noticing, the script
  detects the missing progress heartbeat after `STALL_TIMEOUT` seconds, kills
  ffmpeg and reconnects. This is the failure mode a plain
  `ffmpeg | grep` pipeline cannot recover from.
- `SIGTERM`/`SIGINT` shut down cleanly, so `systemctl stop` is graceful.

## Testing without the real stream

`now_playing.py` runs whatever `--ffmpeg` points at, so a stub script that
prints `    StreamTitle     : Some - Track` lines to stderr and
`progress=continue` to stdout is enough to exercise the parsing, de-duplication,
restart and stall-detection paths offline.
