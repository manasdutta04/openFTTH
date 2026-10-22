# Importing a project's data

openFTTH designs from the data a designer already has, not only from an OpenStreetMap extract.
An **import profile** says which file and layer plays which part in the design, which columns
carry what, and in which CRS a file is when the file does not say. The profile is saved with the
project (`import.yaml`), so the same inputs always rebuild the same twin.

## Formats

Anything GDAL reads through pyogrio: GeoPackage, Shapefile, GeoJSON, KML, FlatGeobuf, GML,
MapInfo, DXF, and CSV with `x`/`y` (or `lon`/`lat`, `easting`/`northing`) or a `WKT` column. Every
layer is reprojected to WGS 84. A CSV with projected coordinates and no CRS must be given one
(for example `EPSG:31370`); the engine refuses to guess.

## Roles

| Role | Geometry | Attributes it understands | Effect on the design |
|---|---|---|---|
| `area` | polygon | `name` | the design area; required |
| `osm` | `.osm` / `.pbf` | — | streets and buildings from OpenStreetMap, used where no other layer gives them |
| `streets` | lines | `id`, `class`, `surface`, `name` | the street graph trenches follow |
| `buildings` | polygons | `id`, `use`, `levels`, `units` | outlines; homes are estimated from them when there is no address register |
| `addresses` | points | `id`, `units`, `kind`, `street`, `housenumber`, `postcode`, `building_id` | the address register: one home per address with its units. It wins over estimates from buildings |
| `existing_ducts` | lines | `id`, `owner`, `free` | cheap routing edges; ducts within 20 m of a street are linked to it |
| `poles` | points | `id`, `owner` | existing poles kept in the design as in-service elements |
| `manholes` | points | `id`, `type` | existing manholes, as above |
| `no_dig_zones` | polygons | `reason` | no new trench inside; existing ducts through the zone stay usable |

Each role's columns are proposed from common names in English, Dutch, French and German. Value
maps (`values`) translate codes, for example a street class `HW` → `primary`.

## Ways in

- **App:** File → Import data… Add files, check the role and columns of each layer, give a
  CRS where the file has none, then *Check data* shows the quality report and *Create project*
  opens the project. Existing ducts, poles and manholes appear on the map under the layer
  *Existing network*.
- **Crew:** Surveyor calls `list_layers`, `propose_import`, `import_data` and `check_inputs`.
  With no extract at all, `fetch_osm(area_path)` downloads one from the Overpass API; this is
  the only network call, and it is named.
- **Command line:** `openftth-engine inspect FILES… --json` proposes a profile;
  `openftth-engine profile-check PROFILE --json` reads it and prints the quality report.
- **Harness:** `POST /api/import/inspect`, `POST /api/import/profile`, and
  `POST /api/project {profile_path}` (see [PROTOCOL.md](PROTOCOL.md)).

## Quality report

`check_inputs` runs before anything is designed and reports, each with a count: homes far from
any street, duplicate address locations, addresses without units, residential buildings without
an address, and a street network in pieces (the share of street vertices the routing cannot
reach). An `error` blocks the design; warnings are for the engineer to read.

## Profile file

```yaml
format: openftth.import
version: 1
name: lambert
bindings:
  - {path: gebied.geojson, role: area}
  - path: adressen.csv
    role: addresses
    crs: EPSG:31370
    x: x
    y: y
    columns: {id: adres_id, units: wooneenheden, street: straatnaam, housenumber: huisnummer}
  - {path: wegen.shp, role: streets, columns: {id: osm_id, class: wegtype}}
  - {path: bestaand.gpkg, layer: kokers, role: existing_ducts, columns: {id: duct_id}}
```

The JSON schema is `profiles/schema.json`, generated with
`uv run openftth-engine schema --profile --out ../profiles/schema.json`.
