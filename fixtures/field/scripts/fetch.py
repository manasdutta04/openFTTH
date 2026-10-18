"""Fetch the field-test areas: real neighbourhoods of different character, straight from the
Overpass API, so the engine can be tried the way an operator would trial a planning tool.

Each area gets a polygon in `fixtures/field/<name>.geojson` (committed, tiny) and an OSM
extract in `fixtures/field/raw/<name>.osm` (git-ignored, re-fetchable). Streets come from a
bbox buffered ~200 m beyond the area so edge homes can snap to the street they face.

Usage:  uv run --project ../../engine python scripts/fetch.py [--only name]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
USER_AGENT = "openFTTH-field-test/0.1"

# name: (south, west, north, east, what it tests)
AREAS: dict[str, tuple[float, float, float, float, str]] = {
    "berlin-pberg": (52.5350, 13.4100, 52.5410, 13.4220, "dense pre-war blocks, large apartment buildings"),
    "amsterdam-jordaan": (52.3720, 4.8780, 52.3790, 4.8880, "canal city: water crossings, narrow streets"),
    "manchester-chorlton": (53.4400, -2.2800, 53.4460, -2.2680, "British terraces, back alleys"),
    "phoenix-grid": (33.6000, -112.0800, 33.6070, -112.0680, "US grid suburb, large lots, long drops"),
    "lillehammer-edge": (61.1100, 10.4600, 61.1180, 10.4750, "Norwegian town edge, detached houses, slopes"),
    "bangalore-indiranagar": (12.9700, 77.6360, 12.9780, 77.6460, "Indian neighbourhood, thin building data"),
    "lochristi-village": (51.0900, 3.8300, 51.1000, 3.8450, "Flemish village, ribbon development"),
}


def query(area: tuple[float, float, float, float]) -> str:
    s, w, n, e = area
    buf_lat, buf_lon = 0.0020, 0.0030
    bs, bw, bn, be = s - buf_lat, w - buf_lon, n + buf_lat, e + buf_lon
    return (
        "[out:xml][timeout:180];("
        f'way["highway"]({bs:.4f},{bw:.4f},{bn:.4f},{be:.4f});'
        f'way["building"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
        f'relation["building"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
        ");(._;>;);out meta;"
    )


def fetch(q: str, name: str) -> bytes:
    """Try each mirror a few times; public Overpass instances answer 502/504 when busy."""
    data = urllib.parse.urlencode({"data": q}).encode()
    last = "no attempt"
    for attempt in range(3):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=240) as resp:
                    body = resp.read()
                if body.startswith(b"<?xml"):
                    return body
                last = f"{url}: unexpected response {body[:120]!r}"
            except Exception as err:  # noqa: BLE001 - any mirror failure just moves on
                last = f"{url}: {err}"
            time.sleep(2)
        time.sleep(10 * (attempt + 1))
    raise SystemExit(f"{name}: all mirrors failed ({last})")


def polygon(area: tuple[float, float, float, float]) -> dict:
    s, w, n, e = area
    ring = [[w, s], [e, s], [e, n], [w, n], [w, s]]
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    for name, (s, w, n, e, what) in AREAS.items():
        if args.only and name != args.only:
            continue
        geo = root / f"{name}.geojson"
        geo.write_text(json.dumps(polygon((s, w, n, e)), indent=1) + "\n", encoding="utf-8")
        out = raw / f"{name}.osm"
        if out.exists() and out.stat().st_size > 10_000:
            print(f"{name}: have {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
            continue
        body = fetch(query((s, w, n, e)), name)
        out.write_bytes(body)
        print(f"{name}: wrote {out.name} ({len(body) / 1e6:.1f} MB) — {what}")
        time.sleep(3)


if __name__ == "__main__":
    main()
