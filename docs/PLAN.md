# openFTTH — Implementation Plan

## 1. Context

openFTTH is a local-first desktop app where an engineer talks to an AI crew and it designs an
FTTH network (routes, chambers, splitters, splice plans, BOQ) from a map, an address list or a
client GIS export. The AI does the work; the human approves every risky step; a licensed
engineer signs the final package. Everything runs on the user's machine; cloud models are
optional.

Target users: small ISPs and municipalities that cannot afford commercial design suites, design
subcontractors, and engineers who want to work an order of magnitude faster.

### Decisions already taken

| Topic | Decision | Reason |
|---|---|---|
| Licence | MIT | simplest for an open tool; all code written for this repo |
| First platform | Windows (Rust) first; macOS (Swift) later from the same Rust crates | the development machine is Windows; the renderer and voice crates are platform-neutral |
| LLM engines | Claude and Codex CLIs are the MVP engines; a fully offline LLM is a Phase 4 stretch goal | a 7–8B local model driving ~30 tools reliably is unproven; offline **voice** stays in the MVP |
| Windows UI | egui on a wgpu (DX12) backend; the 3D twin renders offscreen (colour + depth + id textures) and is shown as an image | a UI toolkit is needed for chat, tables and cards; offscreen rendering keeps depth testing and keeps the renderer crate toolkit-free for the Mac host |
| Design store | per-project GeoJSON bundle written canonically (sorted by id, one feature per line, 7-decimal coordinates), sharded by FDH area from Phase 1, versioned with pure-Python git, diffs computed semantically by element id | undo, versions and audit come for free; canonical writes keep large projects workable |
| Routing | OR-Tools CP-SAT for capacitated placement (cells of ~500 demand points, k-nearest candidates, greedy hint, soft minimum fill, deterministic interleaved search) and NetworkX rooted Steiner trees | zero-install local-first; no spatial database server |
| Engine ↔ agents | one long-lived engine process per project as a streamable-HTTP MCP server; single-writer commit queue; stdio only for tests | several agents share one engine without concurrent writes |
| Tool permissions | explicit per-member allow-lists; risky tools never listed so they raise approval cards; the engine re-validates an `approval_id` before acting | defence in depth against any engine instance running in a permissive mode |

## 2. Architecture

```
┌ openftth-app (Rust, egui + wgpu) ───────────────────────────────────────┐
│ chat column 30% │ workspace 70%: Twin (2D/3D) · BOQ · Splice · Permit · Diff │
│ voice: mic → on-device speech recognition → cleanup → (writer) → chat    │
└──── spawns & owns ──────────────────────┬────────────────────────────────┘
          HTTP + SSE (127.0.0.1:<port>, Bearer OPENFTTH_APP_TOKEN)
┌ harness (Node 24) ───────────────────────┴────────────────────────────────┐
│ crew: Surveyor · Planner · Checker · Drafter (crew members in one project room) │
│ engines: claude CLI · codex │ approvals · memory · backstage · routines  │
└──── MCP (streamable HTTP, one process per project) ┬──────────────────────┘
┌ openftth-engine (Python 3.12) ──────────────────────┴──────────────────────┐
│ ingest · twin · rules · solve · validate · deliver                        │
└──── reads/writes ───────────────────────┬─────────────────────────────────┘
~/.openftth/projects/<id>/  (git repo: design bundle, rulebook.yaml, versions = commits)
~/.openftth/{config.json, messages.db, members/<key>/MEMORY.md, models/, py/}
```

Hard rule: **the LLM never writes numbers into the design.** Every mutation is a tool call that
takes ids and rule references; solvers and validators produce the numbers and record a reason
per decision that the Explain stage narrates.

### Repository layout

```
openFTTH/
  engine/          Python design engine (uv): interchange, ingest, twin, rules, solve, validate, deliver, MCP server, CLI
  harness/         Node 24 agent harness: HTTP + SSE server, crew/room/tasks, engine drivers, approvals, memory, backstage
  desktop/         Cargo workspace: openftth-core, openftth-voice, openftth-render, openftth-client, openftth-ffi, openftth-app
  macos/           SwiftPM package (Phase 4)
  rulebooks/       YAML presets + generated schema.json
  crews/openftth/  crew manifest + crew member instructions
  fixtures/gent/   test area
  docs/            PLAN, PROTOCOL, DESIGN-MODEL, RULEBOOK, DEMO
  .github/workflows/
```

## 3. Components

### 3.1 Engine (`engine/`) — done in Phase 0, extended in Phase 1
- Network model: POP → feeder → FDH (288/576) → distribution → FAT (≤ `fat.max_homes`) → drop → home.
  Centralised or cascaded split; MDUs with more units than a FAT get dedicated indoor FATs; homes
  count dwelling units.
- Rulebook (pydantic → `rulebooks/schema.json`): PON class/budget/differential/cascade, fibre
  ladders and spare, distances, loss model with connectors per path and design margin, FDH/FAT/
  chambers/closures/slack/ducts, surface costs and `highway_to_surface`, unit and labour prices.
- Tools (each returns `{summary, changes, decisions, warnings}` and commits one version):
  ingest `import_osm`, `import_addresses`, `import_gis`; twin `build_twin`; rules `get_rulebook`,
  `set_rule`, `explain_rule`, `list_presets`; solve `set_pop`, `cluster_homes`, `place_fdh`,
  `assign_pon`, `route_feeder`, `route_distribution`, `route_drops`, `place_chambers`,
  `size_cables`, `assign_fibers`, `generate_scenarios`; validate `check_rules`; explain
  `explain_element`, `list_assumptions`; deliver `boq`, `splice_schedule`, `export_gpkg`,
  `export_dxf`, `export_pdf`, `permit_pack`; risky `add_trench`, `route_outside_ducts`,
  `export_for_construction`, `rewind_design`.
- Tests: schema sync, bundle round trip, hand-computed G.984.2 budget cases, solver property
  tests (units conserved, capacities respected, rooted trees, drop limits), byte-identical
  determinism.

### 3.2 Harness (`harness/`) — Phase 1
- Single Node process, no framework: `http` server on 127.0.0.1, Bearer token, loopback checks.
- SSE `/api/events` with sequence ids, 500-frame replay buffer, `Last-Event-ID` resume, ping.
- Data model: crew (from the manifest), crew members with state and markdown memory, one project room,
  tasks (one per user turn) with transcript, approvals and token/cost usage. SQLite via
  `node:sqlite` for messages; atomic JSON for config, project and approvals.
- Engine drivers behind one interface (`sendTurn`, `interrupt`, `respondToApproval`, events):
  Claude CLI (long-lived process, JSON stream, resume, permission prompts routed to the harness),
  Codex CLI. Allowed tools per crew member from the crew manifest.
- Project service: spawns the engine MCP server, proxies versions/diff, tags approved versions,
  exposes `/api/internal/approvals/:id` for the engine.
- Hand-offs: a crew member's reply that mentions `@Teammate` triggers that crew member's turn (bounded hops).
- Backstage: every runtime event appended to NDJSON and streamed.
- Bundle with esbuild; ship with a Node runtime next to the app.

### 3.3 Windows app (`desktop/`) — Phase 1–2
- `openftth-core`: geometry types, rulebook model, design bundle reader, diff model, text
  cleanup pipeline (filler words, self-corrections, numbers and units, learning dictionary),
  hold-to-talk shortcut state machine.
- `openftth-render`: wgpu scene (streets, buildings extruded in 3D, cables, chambers), 2D and
  orbit cameras, id-buffer picking, diff overlay; renders to offscreen textures; no UI toolkit
  dependency.
- `openftth-client`: typed HTTP + SSE client for the harness protocol.
- `openftth-voice`: microphone capture, on-device speech recognition (multilingual model),
  voice activity detection, optional local writer model for formatting, local text-to-speech;
  model packs downloaded once with checksums.
- `openftth-app`: egui window, chat column (messages, tool chips, approval cards, stage
  checklist), workspace tabs (Twin, BOQ, Splice, Permit, Diff, Backstage), sidecar supervisor,
  global hotkey, tray, settings. Packaging: per-user installer plus a one-line PowerShell install.

### 3.4 macOS (`macos/`) — Phase 4
SwiftUI/AppKit shell hosting the Rust renderer through a Metal layer and a UniFFI bridge; speech
through the platform's on-device models; same harness sidecar.

## 4. Roadmap

Timeline assumes one developer plus an AI coding agent full-time.

**Phase 0 — Spike (done).** Headless engine designs the Gent fixture end to end with no rule
violations; one version per tool call; MCP server verified with a client.

**Phase 1 — MVP, text + 2D (weeks 3–10).** Harness (server, SSE, crew, Claude/Codex drivers,
approvals, project service, backstage, tests); address register + building footprints fixture
with unit counts; remaining engine tools (cables sizing, fibre assignment, splice rows,
GeoPackage export, scenarios); Rust workspace with 2D twin, Diff, BOQ and Backstage tabs,
sidecar supervisor; CI for all three stacks.
**Gate 1:** a full Gent-suburb design from polygon to BOQ in the app, exported to GeoPackage and
opened in QGIS; every change visible as a diff; one risky step approved by click; a permissive
engine instance calling `add_trench` without approval is refused.

**Phase 2 — Voice + ship (weeks 11–16).** Dictate mode, command mode on the map selection, call
mode with local text-to-speech and barge-in, Dutch/French/Norwegian/English; scenarios with cost;
splice schedule and PDF/DXF deliverables; installer and release workflow.
**Gate 2 (≈ month 4):** voice-driven design demo on a fresh Windows machine installed via the
one-liner; speech fully offline, LLM via the CLI engines.

**Phase 3 — 3D + perception (weeks 17–24).** 3D twin with terrain and underground view, LAS/LAZ
ingest, asset detection on street imagery with confidence and a "verify in field" list, permit
pack draft.

**Phase 4 — Mac, partners, scale, offline LLM (weeks 25–32).** macOS app; presets validated with
a design partner (cost per home within ±10 % of a real design); routines and webhooks; plugin
docs for community MCP tools; per-FDH solving for 50k+ homes; offline LLM only if it passes a
fixed 20-task benchmark.

## 5. Verification
- Engine: `uv run pytest`; headless design on the fixture in CI with the artefacts uploaded.
- Harness: Node test runner; SSE resume, approval round-trip and allow-list tests against a fake
  engine CLI; packaged-bundle smoke test outside the repo.
- App: `cargo test`, `cargo clippy -D warnings`; renderer snapshot tests on a software adapter;
  manual gate demos scripted in `docs/DEMO.md`.

## 6. Risks
- Input data, not AI, is the biggest risk: underground duct records are closed or wrong. Users
  bring their own data; a confidence layer flags guessed ducts.
- Legal sign-off: the approval and sign-off flow with an audit log of every decision is a
  feature, not an afterthought.
- Country rules differ: pluggable rulebooks with community presets; every value marked
  `source:` or `assumed`.
- Numbers: the LLM never writes them; solvers and validators do.
- Scale: solve per cluster, shard the bundle, cache the twin.
- Unsigned installers will trigger SmartScreen; budget for code signing.
