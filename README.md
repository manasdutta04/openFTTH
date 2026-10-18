# openFTTH

**open** says it is open source and free to run. **FTTH** says exactly what it designs.

openFTTH is an open-source, local-first desktop app where you talk to an AI crew and it
designs a whole fibre-to-the-home network. Give it a map, an address list or a client GIS
export. It hands back a construction-ready design: routes, chambers, splitters, splice
plans and the bill of quantities.

- **The AI does the work.** A crew of four long-lived agents (Surveyor, Planner, Checker,
  Drafter) reads the inputs, places equipment, routes cables, checks the rules and writes
  the deliverables. The LLM plans, explains and talks; deterministic solvers and rule
  checkers make every engineering decision, so every result can be repeated and audited.
  **The LLM never writes a number into the design.**
- **The human approves.** Every design change is a reviewable diff. Leaving a duct, digging
  a new trench or exporting for construction needs one click. A licensed engineer signs
  the final package.
- **Local first.** Everything runs on your machine. Cloud models are optional.

## Status

Phase 1 in progress: the design engine works headless; the agent harness and the Windows app
are being built. Nothing here is usable for real designs yet. See [docs/PLAN.md](docs/PLAN.md)
for the roadmap and [docs/DEMO.md](docs/DEMO.md) for the current gate demo.

## Layout

| Path | What |
|---|---|
| `engine/` | Python design engine: ingest, twin, rules, solve, validate, deliver. Exposed to the agents over MCP, and as a headless CLI. |
| `harness/` | Agent harness: crew members, rooms, engines, approvals, memory, backstage; a local HTTP + SSE sidecar. |
| `rulebooks/` | Design rulebooks (YAML) with JSON schema; country presets. |
| `crews/openftth/` | The four-member crew definition and each crew member's instructions. |
| `fixtures/gent/` | Test area: a Gent suburb from OpenStreetMap. |
| `desktop/` | Rust workspace: core, voice, renderer, Windows app. |
| `macos/` | Swift package for the Mac app (later). |
| `docs/` | Plan, protocol, rulebook and design-model documentation. |

## Quick start (engine only)

```bash
cd engine
uv sync
uv run openftth-engine design --area ../fixtures/gent/area.geojson --osm ../fixtures/gent/gent.osm --rulebook ../rulebooks/belgium.yaml --out ../runs/gent
```

## Licence

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
