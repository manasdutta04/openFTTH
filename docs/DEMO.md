# Gate demos

## Gate 0 — headless engine (passes)

**Claim:** from a polygon and a local OSM file, the engine designs a GPON network for a Gent
suburb (POP, FDHs, FATs, feeder/distribution/drop cables, chambers), validates it, prices it,
and every step is a version.

```bash
cd engine
uv sync
uv run openftth-engine design --area ../fixtures/gent/area.geojson --osm ../fixtures/gent/gent.osm --rulebook ../rulebooks/belgium.yaml --out ../runs/gent --fresh
uv run openftth-engine versions ../runs/gent
```

Look at `runs/gent/deliverables/`: `map.png`, `boq.csv`, `validation.json`,
`splice_schedule.csv` and `design.gpkg` (open it in QGIS: one layer per element type plus the
splice tables). `runs/gent/design/` holds the same as the versioned GeoJSON bundle.

Tests (unit, property and determinism): `uv run pytest`.

## Gate 1 — through the agent crew (in progress)

The Windows app does steps 1–6 for you: `cd desktop && cargo run -p openftth-app` spawns the
harness, offers the Gent fixture in the open-project form, streams the room, shows approval
cards and draws the design as versions arrive. The curl steps below exercise the same API by hand.

1. Start the harness:
   ```bash
   cd harness
   pnpm install
   OPENFTTH_APP_TOKEN=dev pnpm dev
   ```
2. Open a project (the harness starts the engine's MCP server itself):
   ```bash
   curl -s -H "Authorization: Bearer dev" -H "content-type: application/json" \
     -d '{"area_path":"<repo>/fixtures/gent/area.geojson","osm_path":"<repo>/fixtures/gent/gent.osm","rulebook":"belgium"}' \
     http://127.0.0.1:8799/api/project
   ```
3. Follow the live stream in one terminal:
   ```bash
   curl -N -H "Authorization: Bearer dev" http://127.0.0.1:8799/api/events
   ```
4. Send the first turn:
   ```bash
   curl -s -H "Authorization: Bearer dev" -H "content-type: application/json" \
     -d '{"text":"Design this area with the Belgian rulebook."}' http://127.0.0.1:8799/api/room/messages
   ```
5. Watch Surveyor import and build the twin, Planner place POP/FDH/FAT and route, Checker
   validate, Drafter report the BOQ. Every engine tool call shows as `tool.activity`; a risky
   tool raises an `approval` frame that step 6 answers.
6. Answer an approval: `POST /api/approvals/<id> {"decision":"allow"}`.
7. Confirm versions: `GET /api/project/versions`.

Gate 1 passes when step 5 produces the FDH/FAT/route layers in the project's `design/` folder,
the version log shows one commit per tool call, and the same project opens in the Windows app
with the diff overlay.
