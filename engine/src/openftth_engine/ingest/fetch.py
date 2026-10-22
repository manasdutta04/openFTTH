"""Download an OpenStreetMap extract for an area from the public Overpass API. This is the one
network call in the engine, and it only ever runs when a user or crew member asks for it."""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
from pathlib import Path

import geopandas as gpd

from .area import load_area

OVERPASS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "openFTTH/0.1 (+https://github.com)"
STREET_MARGIN_M = 200.0


def overpass_query(area_path: str | Path) -> str:
    """Streets in a margin around the area (homes on the edge face streets just outside it),
    buildings inside its bounding box."""
    area = load_area(area_path)
    s = gpd.GeoSeries([area], crs=4326)
    utm = s.estimate_utm_crs()
    w, so, e, n = s.total_bounds
    bw, bs, be, bn = s.to_crs(utm).buffer(STREET_MARGIN_M).to_crs(4326).total_bounds
    return (
        "[out:xml][timeout:180];("
        f'way["highway"]({bs:.5f},{bw:.5f},{bn:.5f},{be:.5f});'
        f'way["building"]({so:.5f},{w:.5f},{n:.5f},{e:.5f});'
        f'relation["building"]({so:.5f},{w:.5f},{n:.5f},{e:.5f});'
        ");(._;>;);out meta;"
    )


def fetch_osm(area_path: str | Path, out_path: str | Path) -> Path:
    """Fetch the extract for the area into `out_path`, trying each public mirror a few times
    (busy instances answer 429/502/504)."""
    q = overpass_query(area_path)
    data = urllib.parse.urlencode({"data": q}).encode()
    last = "no attempt"
    for attempt in range(3):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=240) as resp:
                    body = resp.read()
                if body.lstrip().startswith(b"<?xml"):
                    out = Path(out_path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(body)
                    return out
                last = f"{url}: unexpected response {body[:120]!r}"
            except Exception as exc:  # noqa: BLE001 — try the next mirror
                last = f"{url}: {exc}"
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"OpenStreetMap download failed: {last}")
