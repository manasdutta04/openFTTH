# Field-test areas

Real neighbourhoods of different character, used to try the engine the way an operator would
trial a planning tool. Only the design polygons are committed; the OpenStreetMap extracts are
large and land in `raw/` (git-ignored). Re-fetch them with:

```bash
uv run --project ../../engine python scripts/fetch.py
```

Then run the engine over all of them from the repo root with `python tools/field_run.py`.
Results and what they showed are in `docs/FIELD-TESTS.md`.

| Area | Bounding box (S, W, N, E) | Character |
|---|---|---|
| berlin-pberg | 52.5350, 13.4100, 52.5410, 13.4220 | dense pre-war blocks, large apartment buildings |
| amsterdam-jordaan | 52.3720, 4.8780, 52.3790, 4.8880 | canal city, water crossings, narrow streets |
| manchester-chorlton | 53.4400, -2.2800, 53.4460, -2.2680 | British terraces, back alleys |
| phoenix-grid | 33.6000, -112.0800, 33.6070, -112.0680 | US grid suburb, large lots, long drops |
| lillehammer-edge | 61.1100, 10.4600, 61.1180, 10.4750 | Norwegian town edge, detached houses, slopes |
| bangalore-indiranagar | 12.9700, 77.6360, 12.9780, 77.6460 | Indian neighbourhood, thin building data |
| lochristi-village | 51.0900, 3.8300, 51.1000, 3.8450 | Flemish village, ribbon development |

Map data © OpenStreetMap contributors, available under the Open Database Licence (ODbL).
