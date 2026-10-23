# openFTTH product plan: a professional FTTH design tool

Status: approved 2026-10-22. Builds on [PLAN.md](PLAN.md) and [PLAN-NETWORK.md](PLAN-NETWORK.md).

## Context
openFTTH today designs a greenfield area automatically from an OpenStreetMap extract, explains every decision, keeps versions, has a network-of-record model (N1), live crew cursors and a free automatic design. A professional designer cannot yet use it as their daily tool: it reads only `.osm` + an area polygon, designs one shape of network (one POP, GPON, underground), has almost no manual drawing tools, prints no plan sheets, and cannot be driven from other AI apps. The user wants it to be the best alternative to incumbent telecom GIS/design tools: everything they do manually, plus the crew doing it automatically, usable in any real-world scenario (no hard-coding), with a modern smooth UI and an MCP server so Claude, ChatGPT and other agents can command and export from it.

Guardrails that stay: the LLM never writes numbers (all edits are engine tools with reasons); risky steps need approval; deterministic outputs; MIT, own code only. "Reverse engineering the incumbents" means studying their **public** documentation, file formats and workflows (and reading open formats with GDAL), never decompiling or copying proprietary software — that would break licences and the repo's own rule.

## Phases (each ends at a measured gate; committed per feature)

### P1 — Inputs from anywhere (engine `ingest/`, app import wizard)
- **Universal reader** `ingest/sources.py` on pyogrio/GDAL (already a dependency): GeoJSON, GeoPackage, Shapefile, KML, FlatGeobuf, CSV with x/y or WKT, DXF; any CRS (read `.prj`/embedded CRS, else ask), reprojected to WGS84; layer listing per file.
- **Import profiles** (`profiles/*.yaml`, pydantic, schema generated): map a source layer to an openFTTH role — `area`, `addresses` (units per address, building ref), `buildings`, `streets`, `existing_ducts`, `poles`, `manholes`, `cabinets`, `no_dig_zones`, `crossings` — with column mappings and value maps. Auto-detect proposes a profile from column names and geometry types; the user confirms; the profile is saved and reused.
- **Address register drives demand**: when addresses are present, homes = addresses (units from the register) instead of building-outline estimates; OSM becomes fallback per gap, recorded as `source: assumption`.
- **Existing infrastructure in the twin**: ducts/poles/manholes become cheap graph edges/nodes (`existing_duct` surface already exists in the rulebook) with capacity attributes; no-dig zones remove edges inside polygons.
- **Data-quality report** tool `check_inputs`: missing streets, homes far from streets, duplicate addresses, CRS doubts, coverage gaps — shown before design.
- **Online fetch is explicit**: `fetch_osm(area)` tool (Overpass, named and opt-in; reuse `fixtures/field/scripts/fetch.py` logic) so users without an extract can start.
- App: **Import wizard** (File → Import data…): drop files, see detected layers on the map, assign roles, fix column mappings, see the quality report, save profile.
- Gate: design three areas from non-OSM sources (a CSV address list + shapefile streets in a non-WGS84 CRS; a GeoPackage with existing ducts; the Gent fixture via OSM) with no code changes.

### P2 — Design breadth (engine `solve/`, rulebook)
- Multiple POPs/OLT sites (given or chosen), POP catchment split.
  *Done:* POPs by capacity, count or given sites, capacitated catchments, feeder and fibres per POP.
- Architectures: centralised split (today), cascaded split (rulebook `pon.cascade` exists), point-to-point; XGS-PON budgets.
  *Done:* centralised, cascaded (second stage in the FATs) and point-to-point, priced side by side as scenarios; XGS-PON (N1–E2 classes, 1270/1577 nm) and GPON + XGS-PON on one ODN with the coexistence loss.
- Route types by rulebook and data: underground trench, existing duct reuse (preferred by cost), aerial on poles, façade along building fronts with wall distribution points and façade→underground transitions; each choice priced and explained.
  *Done:* aerial spans on the pole layer and façade runs where walls line the street, each edge
  priced against its trench, short runs left in the ground, closures mounted on poles/walls,
  pole attachments and transitions in the BOQ; scenarios `trench-only`, `facade`, `aerial`,
  `aerial-and-facade`.
- Phasing: take-rate and build phases (design all, build by phase), BOQ per phase.
  *Done:* `fibers.take_rate` scales the connection costs; `plan_phases` orders cabinets by
  density or distance and the delivery writes a BOQ per phase (trench dug once). Blocks of flats
  get a riser, floor boxes and flat cabling (`inside`).
- Constraints: crossings (rail/water/major road) with permit cost, max cabinet fill per operator pack, keep-outs.
  *Keep-outs done:* keep-out and no-dig zones drawn in the app or imported; equipment stays out
  of keep-out zones, trenches out of both.
  *Crossings done:* priced bores under motorways, railways, rivers and unmapped obstacles,
  taken only when cheaper than going round, with the detour recorded (see RULEBOOK.md).
- Gate: a terraced street gets façade where allowed, ducts reused where present, aerial where poles exist; scenario table compares them.
  *Passed (2026-10-23)*, Gent small area (601 units, belgium rulebook, 602 poles placed along
  every other street; `engine/tests/test_overhead.py`):

  | scenario | trench m | façade m | aerial m | FATs | total EUR | per unit |
  |---|---|---|---|---|---|---|
  | trench-only | 4,134 | 0 | 0 | 36 | 1,009,256 | 1,679 |
  | facade | 2,509 | 883 | 0 | 36 | 872,791 | 1,452 |
  | aerial | 1,343 | 0 | 2,300 | 36 | 760,713 | 1,266 |
  | aerial-and-facade | 1,113 | 338 | 2,170 | 36 | 750,254 | 1,248 |

  No violations in any scenario. Existing ducts are reused by cost as before (the sample has none).

### P3 — Professional manual editing (app + engine edit tools)
- Engine edit tools (all versioned, `source: user`, re-validate): `add_site(kind, lon, lat)`, `delete_element`, `draw_cable(kind, via points snapped to graph)`, `split_cable`, `join_cables`, `reassign_home(home, fat)`, `set_attribute(element, key, value)` within schema, `edit_splice(row)`; undo/redo = version stepping (`rewind_design` stays risky only for posted versions).
- App tool palette on the map (select, move, draw cable, add site, split, measure), snapping to streets/sites, keyboard shortcuts, multi-select, box select, attribute table per layer with inline edit, search by id/name/address, undo/redo (Ctrl+Z/Y), live validation markers on the map.
- Gate: an engineer builds a small network entirely by hand, validates it and posts it.
  *Done:* engine tools `add_site`, `delete_element`, `delete_elements`, `draw_cable`,
  `split_cable` (fibres spliced straight through at the new chamber), `join_cables` (refuses a
  joint that crosses fibres), `edit_splice` (re-patch, splice/patch with the rulebook's loss,
  tray, note, remove), `reassign`, `set_attribute`, `move_site`, `undo_edit`, `redo_edit`,
  `list_violations`; each edit needs a reason, pins what it places and reports the rule
  violations after it. The app has a tool bar on the map (V select, T FAT, H cabinet,
  C chamber, D draw, S split, R reassign, M measure, J join), Shift-click and Shift-drag box
  selection, Del for one or many, Ctrl+Z/Y, rule-issue rings on the map with their messages,
  a Table tab per layer with inline edit, and an edit strip in the Splices tab.
  *Gate passed (2026-10-22):* on a fresh two-block area (`fixtures/gent/area-hand.geojson`,
  62 homes) the network was built in the app by hand only: "Lay out by hand" prepared the area,
  a POP, a cabinet and four FATs were placed, homes and FATs were given to them by box selection
  and Reassign, one feeder and four distribution cables were drawn, and Edit → Complete the
  network routed, sized and numbered it along exactly those cables. The free checks reported no
  violations (worst ODN loss 21.36 dB of 28.0 dB) and no operator-pack violations; WO-0001 was
  opened and posted to the network of record through its approval card.
  `engine/tests/test_hand_built.py` repeats the gate, including a refused post while the layout
  still broke the rules.
  The map has an Auto / Manual switch (E): Manual brings out the tool bar, frames the map in the
  accent colour and shows in the status bar; while a crew member or the automatic design works,
  manual editing is locked (greyed tools, a banner with "Stop the crew", every edit refused).
  Icons are Material Symbols (Apache-2.0, see NOTICE).

### P4 — Plan sheets, reports and exchange (engine `deliver/`, app)
- Plan sheets: atlas of A3/A1 pages over the area (or per block), operator symbology from the operator pack, legend, scale bar, north arrow, title block from work order, label placement without collisions; PDF and DXF (own writers: DXF R12/R2000 text format, PDF via matplotlib backend already present).
- Reports: BOQ (exists), splice and port reports, cable schedule, materials by phase, as-designed vs as-built.
- Exchange profiles: export/import mappings to operator schemas (GeoPackage/Shapefile/CSV field renames, code lists), round-trip tested.
- Gate: a printable plan set + DXF opens in QGIS/LibreCAD; an export profile round-trips.
  *Done (2026-10-23):* `plan_sheets` writes plans.pdf (key plan with the sheet grid, then one
  A3 or A1 sheet per grid cell with network: legend, scale bar, grid north, title block with
  project, sheet n of N, scale, CRS, rulebook, design id, work order and status; labels that
  would overlap are left out and counted) and plans.dxf (own R12 writer, one layer per kind,
  in the design's UTM metres). `reports` writes the cable schedule, the port report and the
  materials by build phase. `export_profile` writes the design through `exports/*.yaml`
  (GeoPackage, Shapefile, CSV; layer and field renames, types, code lists, CRS). Tests: GDAL
  (QGIS's reader) reads every DXF layer back with the feeder's length intact; each shipped
  profile round-trips every mapped value and geometry to within a millimetre; plan sheets,
  reports and exports are byte-identical across two runs of the Gent design. Plan sheets and
  DXF ship as beta in 0.1. Next: operator symbology from the operator pack; as-designed vs
  as-built report.

### P5 — Open to other AI apps: public MCP server (harness)
- `openftth mcp` entry: a **stdio MCP server** for Claude Desktop/Claude Code/Cursor and a **streamable-HTTP endpoint** on the harness (`/mcp`, bearer token, loopback; optional user-started tunnel for web clients such as ChatGPT connectors, which need a public HTTPS URL).
- Exposes curated tools (open project, design automatically, ask the crew, design_summary, trace, check, export deliverables, plan sheets, steer), resources (deliverables as files, current design as GeoJSON) and prompts ("design this area", "review this design"); risky tools still return an approval card to the app — never auto-approved from outside.
- App: Settings → Connect other AI apps (shows the token, copyable config snippets for Claude Desktop/Cursor, on/off).
- Gate: Claude Desktop opens a project, runs the design, and pulls the BOQ through MCP.

### P6 — Autonomy on the device
- Routines: watch folders/inbox for new inputs (area, address list, as-built PDF), run design/validation per a saved recipe, produce packages, put results in an **Approvals inbox**; schedule (daily re-check), notifications.
- Agents choose their own model per crew member (local Ollama for cheap steps, stronger model for review); cost meter per project.
- Gate: dropping a new area file into the watched folder yields a validated design waiting in the inbox with no clicks.

### P7 — As-built intake from PDF (Recorder, plan PLAN-NETWORK N3)
- Vector PDF extraction (paths/text with positions), georeferencing to cadastre/OSM by control points, symbol + label parsing via operator-pack legend, diff against design, review queue. Research track; gate as in PLAN-NETWORK.md.

### P8 — Product quality, scale and polish
- Scale: per-FDH solving and tiled map rendering for 100k homes; background loads everywhere; memory caps.
- Reliability: autosave/recovery, crash log, structured errors in UI, update check.
- UI: design system pass (spacing, type scale, colours, dark/light), onboarding tour, command palette (Ctrl+K), dockable panels, basemap tiles (optional, cached, OSM-licensed attribution), accessibility, i18n (EN, NL, FR, DE first).
- Packaging (later, per user): Windows installer with bundled Python/Node runtimes, signed builds.

## Order and parallelism
P1 → P2 → P3 → P4 are the core product and go first, in that order (P2–P4 depend on P1's data model). P5 (harness-only) is independent and can run in parallel with P1. P6 builds on P5; P7 and P8 follow. Each phase is broken into feature commits with tests; the full plan is also written to `docs/PLAN-PRODUCT.md` so it lives in the repo.

## Critical files
- Engine: `ingest/osm.py`, `ingest/area.py`, new `ingest/sources.py`, `ingest/profiles.py`, `twin/graph.py`, `solve/*`, `pipeline.py`, `mcp_server.py`, `deliver/*`, `rules/model.py`, `network/*`.
- Harness: `src/server.ts`, `src/project.ts`, new `src/mcp-public.ts`, `crews/openftth/members/*.md`.
- App: `app.rs` (menus, wizard host), new `import.rs`, `editor.rs` (tool palette), `twin.rs` (snapping, selection), `tabs.rs` (attribute tables), `state.rs`, `client`.
- Docs: `docs/PLAN-PRODUCT.md`, `PROTOCOL.md`, `RULEBOOK.md`, new `IMPORT.md`, `MCP.md`.

## Verification (per phase)
- Engine: `uv run pytest` incl. new property tests (import round-trips across formats/CRS, determinism with addresses and existing ducts); field-run on the 9 areas still passes.
- Harness: `pnpm test` incl. MCP client tests against `/mcp` and the stdio server.
- App: `cargo clippy -D warnings`, `cargo test`; live checks in the release app from the Start-menu shortcut with screenshots for each gate.
- Gates above are executed and reported with numbers before a phase is called done; commits stay local until the user asks to push.
