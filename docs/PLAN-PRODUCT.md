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
- Architectures: centralised split (today), cascaded split (rulebook `pon.cascade` exists), point-to-point; XGS-PON budgets.
- Route types by rulebook and data: underground trench, existing duct reuse (preferred by cost), aerial on poles, façade along building fronts with wall distribution points and façade→underground transitions; each choice priced and explained.
- Phasing: take-rate and build phases (design all, build by phase), BOQ per phase.
- Constraints: crossings (rail/water/major road) with permit cost, max cabinet fill per operator pack, keep-outs.
- Gate: a terraced street gets façade where allowed, ducts reused where present, aerial where poles exist; scenario table compares them.

### P3 — Professional manual editing (app + engine edit tools)
- Engine edit tools (all versioned, `source: user`, re-validate): `add_site(kind, lon, lat)`, `delete_element`, `draw_cable(kind, via points snapped to graph)`, `split_cable`, `join_cables`, `reassign_home(home, fat)`, `set_attribute(element, key, value)` within schema, `edit_splice(row)`; undo/redo = version stepping (`rewind_design` stays risky only for posted versions).
- App tool palette on the map (select, move, draw cable, add site, split, measure), snapping to streets/sites, keyboard shortcuts, multi-select, box select, attribute table per layer with inline edit, search by id/name/address, undo/redo (Ctrl+Z/Y), live validation markers on the map.
- Gate: an engineer builds a small network entirely by hand, validates it and posts it.

### P4 — Plan sheets, reports and exchange (engine `deliver/`, app)
- Plan sheets: atlas of A3/A1 pages over the area (or per block), operator symbology from the operator pack, legend, scale bar, north arrow, title block from work order, label placement without collisions; PDF and DXF (own writers: DXF R12/R2000 text format, PDF via matplotlib backend already present).
- Reports: BOQ (exists), splice and port reports, cable schedule, materials by phase, as-designed vs as-built.
- Exchange profiles: export/import mappings to operator schemas (GeoPackage/Shapefile/CSV field renames, code lists), round-trip tested.
- Gate: a printable plan set + DXF opens in QGIS/LibreCAD; an export profile round-trips.

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
