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
  and registers it as the folks' MCP server `engine = {url: "http://127.0.0.1:<p>/mcp"}`. All
  four folks connect to that one URL. Risky engine tools validate `approval_id` against
  `GET /api/internal/approvals/:id` (Bearer `OPENFTTH_COMMS_TOKEN`), which the harness passes
  to the engine as `OPENFTTH_APPROVALS_URL`.

## HTTP API (v1)

| Method & path | Purpose |
|---|---|
| `GET /api/health` | `{ok, version, uptime_s}`; no auth |
| `GET /api/events?since=<cursor>` | SSE stream, resumable with `Last-Event-ID`; first frame `hello` |
| `GET /api/crew` | the crew manifest with per-folk state |
| `GET /api/rulebooks` | the shipped rulebook presets (name, description, country) |
| `GET /api/projects` | projects opened before, newest first |
| `GET /api/engines` | the folk engines (Claude CLI, Codex CLI, Ollama, OpenRouter, OpenAI-compatible): availability, models, which one is active |
| `POST /api/engine` | `{kind, model?, baseUrl?, apiKey?}` switch the crew's engine; folk sessions restart on the next turn |
| `POST /api/project {area_path, osm_path, rulebook}` | create or open a project: starts the engine, creates the room |
| `GET /api/project` | project meta, stage, head version, engine status |
| `GET /api/project/versions` | version log (from the engine) |
| `GET /api/project/diff?from&to` | semantic diff by element id (from the engine) |
| `GET /api/project/element/:uuid` | the engine's explanation of one element (attributes + recorded decisions) |
| `GET /api/project/assumptions` | every assumption the engine recorded |
| `POST /api/project/approve {version}` | tag a version as approved |
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
| `message` | `{id, folk, role, text, at, kind}` | chat column |
| `message.patch` | `{id, textDelta}` | streaming text |
| `tool.activity` | `{folk, tool, state: started|done|failed, summary?}` | "routing 14 feeders…" chips |
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

Risky tools return `{"error": "approval_required" | "approval_unverifiable" | "approval_denied", ...}`
instead of acting.

## Per-folk tool allow-lists

The crew manifest carries a `tools` array per member. The harness passes `mcp__engine__<tool>`
entries as the allowed tools of that folk's engine process and strips file-editing and shell
tools from Surveyor, Planner and Checker. Risky tools are never listed, so they raise approval
cards naturally; the engine re-checks the approval id as the last line of defence.
