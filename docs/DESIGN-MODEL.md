# Design model

## Network hierarchy

```
POP / OLT ─ feeder ─► FDH (splitter cabinet, 288/576 ports) ─ distribution ─► FAT (≤ fat.max_homes units) ─ drop ─► home / ONT
```

- **Centralised split** (default): one 1:N splitter per PON sits in the FDH; every home has its
  own fibre back to the FDH, so distribution fibre count = downstream units + spare.
- **Cascaded split** (`pon.cascade: [4, 8]`): an L1 splitter in the FDH and an L2 splitter in
  the FAT. The budget check sums both stages.
- **MDU**: a building with more units than one FAT gets dedicated indoor FATs.
- Every home carries `pon_id`, `fdh_id`, `fat_id` (and `fat_ids` for MDUs) in its
  `openftth` extension.

## Storage: the design bundle

A project is a directory and a git repository (pure-Python git, no `git.exe` needed):

```
<project>/
  project.json          id, stage, created_at
  rulebook.yaml         the rulebook used
  design/               GeoJSON design bundle (canonical, one feature per line)
    _openftth_metadata.json
    building.geojson  service_area.geojson  odf.indoor.geojson  otb.outdoor.geojson
    tb.outdoor.geojson  tb.indoor.geojson  to.indoor.geojson  cable.underground.geojson
    manhole.geojson
  twin/streets.geojson  street graph with surfaces, for the renderer
  deliverables/         boq.json  boq.csv  map.png  summary.json  validation.json
  decisions.ndjson      every decision with its reason and values
```

Each feature carries `element_type`, `placement` (indoor | outdoor | pole | closure),
`element_uuid` (≤ 64 chars, stable) and `extra_json`; openFTTH-specific attributes live under
`extra_json["openftth"]`. Metadata keys: `format=openftth-design`, `format_version=1.0`,
`schema_version`, `produced_by`, `produced_at`, `crs_epsg=4326`, `color_standard` (e.g.
`TIA-598-C`), `profile`. Readers accept any 1.x and refuse an unknown major version.

| openFTTH element | `element_type` | placement | `openftth.kind` |
|---|---|---|---|
| POP / ODF | `odf` | indoor | `pop` |
| FDH | `otb` | outdoor | `fdh` |
| FAT | `tb` | outdoor / indoor (MDU) | `fat` |
| Home (ONT side) | `to` | indoor | `home` |
| Feeder / distribution / drop | `cable.underground` | — | `feeder` / `distribution` / `drop` |
| Chamber | `manhole` | — | `chamber` |
| Building, area | `building`, `service_area` | — | `building`, `service_area` |

Element types available: lines `route`, `cable.aerial`, `cable.underground`, `duct.pe`,
`duct.transition`; points `pole`, `manhole`, `closure.joint`, `odf`, `otb`, `to`, `tb`,
`patch_panel`, `slack`, `fiber_break`; polygons `service_area`, `building`.

## Side tables

`<name>.table.json` files next to the layers, one row per line sorted by `uuid`:

| Table | Row | Written by |
|---|---|---|
| `splice_point` | `uuid, element_uuid` (chamber, FAT, FDH or POP), `port_count, used_ports, config_json {tree, node}, notes` | `assign_fibers` |
| `fiber_connection` | `uuid, splice_point_uuid, source_{element_uuid, cable_uuid, tube, fiber, port, side}, dest_{…}, connection_type` (splice, splitter or patch), `loss_db, tray_ref, notes` | `assign_fibers` |

Fibres are numbered depth-first from each FDH (and from the POP for the feeder), so every
section carries a contiguous range (`openftth.fiber_start` / `fiber_end`) and its local
numbering starts at 1. Tubes hold 12 fibres and colours follow `color_standard` (TIA-598-C).
Each home records `fiber_start` / `fiber_end` and its `splitter_port`.

## Exports

`export_gpkg` writes `deliverables/design.gpkg`: one layer per bundle file (dots in names become
underscores), openFTTH attributes as an `extra_json` column, the side tables and
`_openftth_metadata` as attribute tables. `splice_schedule` writes `splice_schedule.csv`.

`element_uuid` is `uuid5(namespace, project|type|local key)` and never changes between runs.

## Versions and diffs

Every tool call commits once: `[stage] tool: summary` with a JSON trailer. `versions` lists
them, `diff_versions(older, newer)` returns added/removed/modified ids by identity (not by text).
Rewinding is a risky tool (approval required) and is itself a new commit.

## Determinism

Same inputs → same bundle bytes (except `produced_at`). CP-SAT runs with a fixed worker count,
interleaved search and a fixed seed; the k-means cell partition uses a fixed seed; Steiner
trees are deterministic. `tests/test_pipeline_gent.py::test_determinism_same_inputs_same_bundle`
guards this.
