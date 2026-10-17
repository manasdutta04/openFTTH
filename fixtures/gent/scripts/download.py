"""Re-download the Gent (Mariakerke) fixture from the Overpass API.

Streets come from a bbox buffered ~200 m beyond the design area so homes on the edge can snap
to the street they face; buildings come from the design area itself.

Usage:  uv run --project ../../engine python scripts/download.py --out .
"""

from __future__ import annotations

import argparse
import urllib.parse
import urllib.request
from pathlib import Path

AREA = (51.0700, 3.6750, 51.0780, 3.6900)  # south, west, north, east
BUFFER_DEG = (0.0020, 0.0030)  # ≈ 220 m lat, 210 m lon at 51° N
OVERPASS = "https://overpass-api.de/api/interpreter"
USER_AGENT = "openFTTH-fixture/0.1"


def query(area: tuple[float, float, float, float]) -> str:
    s, w, n, e = area
    bs, bw, bn, be = s - BUFFER_DEG[0], w - BUFFER_DEG[1], n + BUFFER_DEG[0], e + BUFFER_DEG[1]
    return (
        "[out:xml][timeout:120];("
        f'way["highway"]({bs:.4f},{bw:.4f},{bn:.4f},{be:.4f});'
        f'way["building"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
        f'relation["building"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});'
        ");(._;>;);out meta;"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("."))
    args = ap.parse_args()
    data = urllib.parse.urlencode({"data": query(AREA)}).encode()
    req = urllib.request.Request(OVERPASS, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read()
    if not body.startswith(b"<?xml"):
        raise SystemExit(f"unexpected response: {body[:200]!r}")
    out = args.out / "gent.osm"
    out.write_bytes(body)
    print(f"wrote {out} ({len(body) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
