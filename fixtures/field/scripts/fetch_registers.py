"""Fetch two official, open address registers for field areas, to test designing from a real
register rather than from homes estimated out of OpenStreetMap buildings.

- amsterdam-jordaan: the Dutch BAG (Basisregistratie Adressen en Gebouwen) through PDOK's WFS,
  residential units in use (`gebruiksdoel` contains `woonfunctie`). Open data (CC0).
- lochristi-village: the Flemish Adressenregister through the Flemish government's WFS,
  addresses in use (`AdresStatus` = `InGebruik`). Open data (Modellicentie Gratis Hergebruik).

Each writes `raw/<area>-addresses.geojson` (git-ignored) with columns `id`, `street`,
`housenumber`, `postcode`, `units` (1 per address), and the committed profile
`<area>-register.profile.yaml`: the area, the register as addresses, and the area's OSM extract
for streets and buildings. Paths in the profile are relative to it.

Usage:  uv run --project ../../engine python scripts/fetch_registers.py [--only name]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "openFTTH-field-test/0.1"
PAGE = 1000

# name: (WFS endpoint, layer, keep(properties) -> bool, map(properties) -> columns)
REGISTERS = {
    "amsterdam-jordaan": (
        "https://service.pdok.nl/lv/bag/wfs/v2_0",
        "bag:verblijfsobject",
        lambda p: "woonfunctie" in str(p.get("gebruiksdoel", "")) and "in gebruik" in str(p.get("status", "")).lower(),
        lambda p: {
            "id": str(p.get("identificatie", "")),
            "street": p.get("openbare_ruimte"),
            "housenumber": f"{p.get('huisnummer', '')}{p.get('huisletter') or ''}{('-' + str(p['toevoeging'])) if p.get('toevoeging') else ''}",
            "postcode": p.get("postcode"),
            "units": 1,
            "building_id": p.get("pandidentificatie"),
        },
    ),
    "lochristi-village": (
        "https://geo.api.vlaanderen.be/Adressenregister/wfs",
        "Adressenregister:Adres",
        lambda p: p.get("AdresStatus") == "InGebruik",
        lambda p: {
            "id": str(p.get("ObjectId", "")),
            "street": p.get("Straatnaam"),
            "housenumber": f"{p.get('Huisnummer') or ''}{(' bus ' + str(p['Busnummer'])) if p.get('Busnummer') else ''}",
            "postcode": p.get("PostinfoObjectId"),
            "units": 1,
        },
    ),
}


def bbox_of(geojson: Path) -> tuple[float, float, float, float]:
    ring = json.loads(geojson.read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"][0]
    lons = [c[0] for c in ring]
    lats = [c[1] for c in ring]
    return min(lats), min(lons), max(lats), max(lons)


def get(url: str) -> dict:
    last = "no attempt"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except Exception as err:  # noqa: BLE001 - retried, then reported
            last = str(err)
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"{url[:80]}…: {last}")


def hits(endpoint: str, params: dict) -> int | None:
    """How many features the server says match (WFS resultType=hits), or None if it won't say."""
    q = urllib.parse.urlencode({**params, "resultType": "hits"})
    req = urllib.request.Request(f"{endpoint}?{q}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        m = re.search(rb'numberMatched="(\d+)"', resp.read())
    return int(m.group(1)) if m else None


def fetch(endpoint: str, layer: str, bbox: tuple[float, float, float, float]) -> list[dict]:
    """Every feature in the box. Servers cap a page below what was asked (PDOK returns fewer
    than `count`), so paging runs until the server's own match count is reached."""
    s, w, n, e = bbox
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "srsName": "EPSG:4326",
        "bbox": f"{s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326",
    }
    matched = hits(endpoint, params)
    out: list[dict] = []
    while True:
        q = urllib.parse.urlencode(
            {**params, "outputFormat": "application/json", "count": PAGE, "startIndex": len(out)}
        )
        page = get(f"{endpoint}?{q}").get("features", [])
        out.extend(page)
        if not page or (matched is not None and len(out) >= matched) or (matched is None and len(page) < PAGE):
            if matched is not None and len(out) < matched:
                raise SystemExit(f"{layer}: got {len(out)} of {matched} features")
            return out
        time.sleep(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    for name, (endpoint, layer, keep, columns) in REGISTERS.items():
        if args.only and name != args.only:
            continue
        area = root / f"{name}.geojson"
        feats = fetch(endpoint, layer, bbox_of(area))
        kept = [
            {"type": "Feature", "geometry": f["geometry"], "properties": columns(f["properties"])}
            for f in feats
            if f.get("geometry") and keep(f.get("properties") or {})
        ]
        out = raw / f"{name}-addresses.geojson"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": kept}), encoding="utf-8")
        profile = root / f"{name}-register.profile.yaml"
        profile.write_text(
            "format: openftth.import\n"
            "version: 1\n"
            f"name: {name}-register\n"
            f"description: {name} from its official address register, streets and buildings from OSM\n"
            "bindings:\n"
            f"- {{path: {name}.geojson, role: area}}\n"
            f"- path: raw/{name}-addresses.geojson\n"
            "  role: addresses\n"
            "  columns: {id: id, units: units, street: street, housenumber: housenumber, postcode: postcode"
            + (", building_id: building_id" if name == "amsterdam-jordaan" else "")
            + "}\n"
            f"- {{path: raw/{name}.osm, role: osm}}\n",
            encoding="utf-8",
        )
        print(f"{name}: {len(kept)} of {len(feats)} register entries kept -> {out.name}, {profile.name}")


if __name__ == "__main__":
    main()
