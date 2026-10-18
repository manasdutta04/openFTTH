# Rulebooks

A rulebook is a YAML file holding every number a solver or validator may use. The agents can
read it, explain it and change single values on the user's request; they can never bypass it.

- Schema: [`rulebooks/schema.json`](../rulebooks/schema.json), generated from
  `engine/src/openftth_engine/rules/model.py` with `openftth-engine schema --out rulebooks/schema.json`.
  `tests/test_rulebook.py` fails when the file is out of date.
- Presets: `generic-gpon` (neutral), `belgium` (Flanders draft), `norway` (draft, cascaded split).
- Validate: `uv run openftth-engine rules validate rulebooks/belgium.yaml`.

## Provenance

Every value in a preset carries a comment: `# source: …` for a public specification or
datasheet, `# assumed` otherwise. A design partner reviews the assumed values before a preset is
called anything but "draft".

## Sections

| Section | Used by | Notes |
|---|---|---|
| `pon` | validate (power budget), assign_pon | ITU-T G.984.2 class (B+ 13–28 dB, C+ 17–32 dB), split ratio, optional cascade `[4, 8]`, differential loss, worst-case wavelength (1310 nm) |
| `fibers` | size cables | ladders for distribution/feeder, drop fibres, spare %, take rate |
| `homes` | ingest | count dwelling units (not addresses); apartment estimation when no unit data |
| `distances` | cluster_homes, place_fdh, validate | max drop (street distance), max FDH→FAT, logical reach, pulling distance |
| `loss_db`, `connectors_per_path` | validate | connector/splice/splitter losses, fibre attenuation per wavelength, design margin, connectors counted on a path (ODF, FDH in/out, FAT, ONT) |
| `fdh`, `fat`, `chambers`, `closures`, `slack`, `ducts` | solve | capacities, fill limits, placements, sizes |
| `routing` | twin | OSM highway classes no cable may follow (motorways, staircases, planned roads, …); those ways never enter the route graph |
| `surfaces`, `highway_to_surface`, `crossings` | twin, boq | trench + reinstatement cost per metre by surface; which surface each OSM highway class trenches in |
| `units`, `labour` | boq | unit prices |

## Power budget formula (validate)

```
ODN loss = km × fiber_per_km[worst_case_nm]
         + connectors_on_path × loss_db.connector
         + splices_on_path × loss_db.splice
         + Σ splitter losses over the cascade
         + loss_db.margin
must be ≤ pon.budget_db, and within one PON max − min ≤ pon.max_differential_db
```

Splice count is derived from the path (FAT, FDH ×2, plus one per branch closure passed), not a
constant.
