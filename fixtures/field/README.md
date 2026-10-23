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
| paris-11e | 48.8580, 2.3730, 48.8630, 2.3820 | Haussmann blocks, very large apartment buildings |
| madrid-salamanca | 40.4250, -3.6850, 40.4300, -3.6770 | grid of courtyard apartment blocks |
| tokyo-setagaya | 35.6450, 139.6500, 35.6500, 139.6580 | tiny plots, narrow lanes, dense detached homes |
| saopaulo-vilamadalena | -23.5600, -46.6950, -23.5540, -46.6870 | hilly, irregular streets |
| nairobi-kilimani | -1.2950, 36.7820, -1.2890, 36.7900 | patchy map data, gated compounds |
| houston-katy | 29.7800, -95.7800, 29.7870, -95.7700 | cul-de-sac suburb, curving streets |
| galway-rural | 53.3300, -9.2000, 53.3550, -9.1600 | scattered one-off houses on country lanes, ~3 km |
| brussels-schaerbeek | 50.8550, 4.3700, 50.8730, 4.3950 | about 2 km of dense city: the large-area test |

## Official address registers

Two areas are also designed from their government's open address register instead of homes
estimated from OSM buildings, through an import profile (`<area>-register.profile.yaml`, paths
relative to it). Fetch the registers into `raw/` with:

```bash
uv run --project ../../engine python scripts/fetch_registers.py
```

| Profile | Register | Kept |
|---|---|---|
| amsterdam-jordaan-register | Dutch BAG (PDOK WFS), residential units in use, with their building id | flats in one building are one home with several units |
| lochristi-village-register | Flemish Adressenregister (WFS), addresses in use | addresses at the same point are one home |

Map data © OpenStreetMap contributors, available under the Open Database Licence (ODbL).
Address data: BAG © Kadaster (CC0, via PDOK); Adressenregister © Digitaal Vlaanderen (Modellicentie
Gratis Hergebruik).
