#!/usr/bin/env python3
"""
charts.py — writes the Apple Music top-10 for a few countries as one JSON file
for the site's "Мировые чарты" block.

Run on Mondays and Fridays (charts.timer). For each country it reads Apple's public
most-played feed, then looks the ten tracks up by their catalogue id in the
same storefront, so the preview clip is exactly the charted track — no fuzzy
search. If a country fails to fetch, its previous list is kept, so a hiccup at
Apple never blanks a column on the site.

    {
      "updated_at": "2026-10-28T03:00:00+00:00",
      "charts": [
        {"cc": "gb", "name": "Великобритания", "updated": "<feed date>",
         "tracks": [{"rank": 1, "artist": "...", "title": "...",
                     "artwork": "...600x600bb.jpg", "preview_url": "...m4a"}]}
      ]
    }

Requires: python3 (stdlib only).
Usage:    ./charts.py --output /var/www/nowplaying/charts.json
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# Order here is the column order on the site. To change countries, edit this.
COUNTRIES = [
    ("gb", "Великобритания"),
    ("us", "США"),
    ("ru", "Россия"),
]
TOP_N = 10
FEED = "https://rss.marketingtools.apple.com/api/v2/{cc}/music/most-played/{n}/songs.json"
LOOKUP = "https://itunes.apple.com/lookup?country={cc}&id={ids}"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_json(url, attempts=3):
    # Apple's feed host occasionally answers a redirect with a 502; a retry
    # a few seconds later goes through.
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "capital-charts/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            if i == attempts - 1:
                raise
            log(f"retry {i + 1} for {url}: {e!r}")
            time.sleep(5 * (i + 1))


def fetch_country(cc, name):
    feed = get_json(FEED.format(cc=cc, n=TOP_N))["feed"]
    entries = feed["results"][:TOP_N]
    found = get_json(LOOKUP.format(cc=cc, ids=",".join(e["id"] for e in entries)))["results"]
    by_id = {str(x.get("trackId")): x for x in found}
    tracks = []
    for rank, e in enumerate(entries, 1):
        hit = by_id.get(e["id"], {})
        art = hit.get("artworkUrl100") or e.get("artworkUrl100")
        tracks.append({
            "rank": rank,
            "artist": e["artistName"],
            "title": e["name"],
            "artwork": art.replace("100x100bb", "600x600bb") if art else None,
            "preview_url": hit.get("previewUrl"),
        })
    return {"cc": cc, "name": name, "updated": feed.get("updated"), "tracks": tracks}


def write_atomic(path, payload):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main():
    p = argparse.ArgumentParser(description="Write Apple Music top-10 charts as JSON.")
    p.add_argument("--output", default=os.environ.get("CHARTS_FILE", "/var/www/nowplaying/charts.json"))
    args = p.parse_args()

    try:
        with open(args.output, encoding="utf-8") as f:
            previous = {c["cc"]: c for c in json.load(f).get("charts", [])}
    except (OSError, ValueError):
        previous = {}

    charts, failed = [], 0
    for cc, name in COUNTRIES:
        try:
            chart = fetch_country(cc, name)
            missing = sum(1 for t in chart["tracks"] if not t["preview_url"])
            log(f"{cc}: {len(chart['tracks'])} tracks, {missing} without preview")
        except Exception as e:
            failed += 1
            chart = previous.get(cc)
            log(f"{cc}: fetch failed ({e!r}); {'keeping previous list' if chart else 'no previous list'}")
        if chart:
            charts.append(chart)

    if not charts:
        log("nothing to write")
        return 1
    write_atomic(args.output, {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "charts": charts,
    })
    log(f"wrote {args.output}")
    return 1 if failed == len(COUNTRIES) else 0


if __name__ == "__main__":
    sys.exit(main())
