# Protocol: app ↔ harness ↔ engine

## Processes

```
openftth-app (Rust)  ──HTTP+SSE──►  harness (Node 24)  ──MCP (streamable HTTP)──►  openftth-engine serve
```

- The app spawns the harness with `OPENFTTH_APP_TOKEN` (random per launch) and
  `OPENFTTH_DATA_DIR` (default `~/.openftth`). Every `/api/*` request except `/api/health`
  carries `Authorization: Bearer <token>`. The server listens on `127.0.0.1` only and
  rejects non-loopback `Host`/`Origin` headers.
- The harness spawns one `openftth-engine serve --project <dir> --port <p>` per open project
  and registers it as the crew members' MCP server `engine = {url: "http://127.0.0.1:<p>/mcp"}`. All
  four crew members connect to that one URL. Risky engine tools validate `approval_id` against
  `GET /api/internal/approvals/:id` (Bearer `OPENFTTH_COMMS_TOKEN`), which the harness passes
  to the engine as `OPENFTTH_APPROVALS_URL`.

## HTTP API (v1)

| Method & path | Purpose |
|---|---|
| `GET /api/health` | `{ok, version, uptime_s}`; no auth |
| `GET /api/events?since=<cursor>` | SSE stream, resumable with `Last-Event-ID`; first frame `hello` |
| `GET /api/crew` | the crew manifest with per-member state |
| `GET /api/rulebooks` | the shipped rulebook presets (name, description, country) |
| `GET /api/projects` | projects opened before, newest first |
| `DELETE /api/project?purge=1` | close the project and delete its design, versions, conversation and runtime log (a reset) |
| `GET /api/engines` | the crew member engines (Claude CLI, Codex CLI, Hermes, Ollama, OpenRouter, OpenAI-compatible): availability, models, which one is active, and what each costs (`cost`: free, free-tier, subscription, paid; `costNote`) |
| `POST /api/engine` | `{kind, model?, baseUrl?, apiKey?}` switch the crew's engine; crew member sessions restart on the next turn |
| `POST /api/project {area_path, osm_path, rulebook}` | create or open a project: starts the engine, creates the room |
| `POST /api/project {profile_path, rulebook}` | create or open a project from an import profile (see [IMPORT.md](IMPORT.md)); an `area_path` ending in `.yaml` is read as a profile, so Recent reopens it. Meta carries `profilePath` |
| `POST /api/import/inspect {paths}` | the layers in data files (geometry, count, CRS, columns, sample values) and the import profile the engine proposes for them, plus `roles` (each role's label and the attributes it understands). `422` with the engine's words when a file cannot be read |
| `POST /api/import/profile {name, profile, rulebook?}` | save the profile to `<dataDir>/profiles/<name>.yaml`, read its data and return `{path, report}`; the report is the data-quality check (`level` ok, info, warning or error; `counts`; `issues`). `422` when the data cannot be read |
| `GET /api/import/profiles` | the saved import profiles `{name, path}` |
| `GET /api/mcp` | the MCP link other AI apps add: `{enabled, listening, error, port, link, requireKey, installs}`; `installs` holds the Claude Code command and the Cursor and VS Code install links (see [MCP.md](MCP.md)) |
| `POST /api/mcp {enabled?, requireKey?, rotate?, port?}` | change the link and start or stop its listener; answers like `GET` |
| `POST /api/mcp/claude-bundle` | write `<dataDir>/openftth.mcpb` for Claude Desktop and return `{path}` |
| `GET /api/project` | project meta, stage, head version, engine status |
| `GET /api/project/versions` | version log (from the engine) |
| `GET /api/project/diff?from&to` | semantic diff by element id (from the engine) |
| `GET /api/project/element/:uuid` | the engine's explanation of one element (attributes + recorded decisions) |
| `GET /api/project/trace/:uuid` | a home's fibre path from OLT port to drop, hop by hop with loss (the engine's `trace_fibre`) |
| `GET /api/project/assumptions` | every assumption the engine recorded |
| `POST /api/project/approve {version}` | tag a version as approved |
| `POST /api/project/autodesign` | design the open project from its inputs to deliverables with the engine alone, no LLM and no cost: every engine tool in stage order. Returns `202 {steps}` at once; each step arrives as a `tool` message, `design.version` and `design.stage` frames. With an import profile the first steps are `import_data` and `check_inputs`. `400` without a map extract or a profile, `409` while the crew is working |
| `POST /api/project/steer {tool, args, revalidate?}` | the engineer's own change through the app: `move_site`, `exclude_building` or `forbid_street`, called on the engine directly (no crew member in the loop). The room gets a `steer` message, the app a `design.version` frame, and the Checker is asked to validate again unless `revalidate` is `false`. `409` while the crew is working, `422` with the engine's words when it rejects the change |
| `POST /api/room/messages {text, designContext}` | a user turn in the project room |
| `POST /api/room/interrupt` | stop the running turn |
| `GET /api/room/messages?since` | transcript page |
| `POST /api/approvals/:id {decision: allow|deny, note?}` | answer an approval card |
| `GET /api/internal/approvals/:id` | engine-side check (comms token) |
| `GET /api/backstage?limit` | recent runtime events (tool calls, errors) |

`designContext = {selection: [element_uuid], cursor: [lon, lat], stage, activeVersion}` is rendered
as a bracketed prefix on the turn text. The system prompt stays stable so the engine CLI process
can be reused across turns.

## SSE frames

Every frame has `id: <stream>:<seq>`, `event: <kind>` and a JSON `data`. The server keeps a
replay buffer of 500 frames; a 15 s `ping` keeps the connection alive.

| kind | payload | app use |
|---|---|---|
| `hello` | `{cursor, resumed}` | first frame |
| `message` | `{id, folk, role, text, at, kind}`; `kind` is `text`, `tool`, `approval`, `handoff`, `error` or `steer` (the engineer's own change, `json.reran` lists the solvers the engine ran again) | chat column |
| `message.patch` | `{id, textDelta}` | streaming text |
| `tool.activity` | `{folk, tool, state: started|done|failed, summary?, target?}`; `folk` is null for the automatic design and `"you"` for the engineer's own change; `target` locates a started tool on the map (`element_uuid`, `lon`/`lat`, `osm_id`, `element_uuids`) | status line, crew cursors |
| `approval` | `{id, folk, tool, summary, cost?, state}` | inline approval card |
| `design.version` | `{version, tool, summary, changes}` | reload bundle, Diff tab |
| `design.stage` | `{stage}` | stage checklist |
| `runtime` | `{folk, event, detail}` | Backstage |
| `engine` | `{kind, label, model, baseUrl, hasKey}` | the active engine changed |
| `folk` | `{key, state: idle|thinking|tooling|waiting}` | presence |

## Engine tool contract

```json
{"tool": "...", "summary": "one line", "changes": {"added": 0, "removed": 0, "modified": 0},
 "decisions": [{"stage": "...", "tool": "...", "element": "uuid|null", "reason": "...", "values": {}, "source": "solver|rulebook|assumption|user"}],
 "warnings": [], "version": "<commit>", "data": {}}
```

Risky tools are `add_trench`, `route_outside_ducts`, `export_for_construction`, `rewind_design`
and `post_work_order`. They return `{"error": "approval_required" | "approval_unverifiable" | "approval_denied", ...}`
instead of acting.

## Per-crew member tool allow-lists

The crew manifest carries a `tools` array per member. The harness passes `mcp__engine__<tool>`
entries as the allowed tools of that crew member's engine process and strips file-editing and shell
tools from Surveyor, Planner and Checker. Risky tools are never listed, so they raise approval
cards naturally; the engine re-checks the approval id as the last line of defence.

The MCP link itself is not on this port: it listens on `127.0.0.1:3920/mcp` (streamable HTTP, stateless) while it is on, loopback only, with the key in the link when one is required.
