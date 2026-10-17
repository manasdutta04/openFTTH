# Gent fixture (Mariakerke sample)

A ~1 km² residential suburb of Gent, Belgium, used as the first openFTTH test area.

| File | What | Source / licence |
|---|---|---|
| `area.geojson` | Design area polygon (bbox 3.675–3.690 E, 51.070–51.078 N) | hand-drawn |
| `gent.osm` | OpenStreetMap extract: all `highway` ways in a ~200 m buffered bbox and `building` ways/relations in the area, with nodes | © OpenStreetMap contributors, ODbL. Overpass snapshot 2026-10-17. |
| `scripts/download.py` | Re-downloads the extract | — |

Phase 0 uses building centroids as synthetic homes. Phase 1 replaces them with the regional
address register (one unit per dwelling number) and official building footprints, which are
too large to commit and land in `raw/` (git-ignored).

Re-download with:

```bash
uv run --project ../../engine python scripts/download.py --out .
```
