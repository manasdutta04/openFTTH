# openFTTH harness

The agent harness: a local HTTP + SSE sidecar that runs the design crew (Surveyor, Planner,
Checker, Drafter), drives one LLM CLI process per folk, turns risky tool calls into approval
cards, keeps per-folk memory, and records every runtime event for Backstage. The native app
spawns it and talks to it over `127.0.0.1`.

- Node 24, ESM, TypeScript run directly by Node (type stripping); no HTTP framework.
- `pnpm dev` runs `src/index.ts`; `pnpm build` bundles to `dist/` (server + permission proxy).
- `pnpm test` runs the end-to-end suite against a fake folk CLI and a fake design-engine MCP
  server, so it needs neither network access nor an LLM.
- Protocol: see [`docs/PROTOCOL.md`](../docs/PROTOCOL.md).

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `OPENFTTH_APP_TOKEN` | required | Bearer token for every `/api/*` route except `/api/health` |
| `OPENFTTH_DATA_DIR` | `~/.openftth` | messages.db, projects/, folks/ memory, backstage/ |
| `OPENFTTH_PORT` | `8799` | `0` picks a free port (printed on stderr as `listening on …`) |
| `OPENFTTH_CREW` | `crews/openftth/crew.openftth.json` | crew manifest |
| `OPENFTTH_ENGINE_COMMAND` / `OPENFTTH_ENGINE_CWD` | `uv run openftth-engine` in `engine/` | how to start the design engine |
| `OPENFTTH_FOLK_ENGINE_COMMAND` | `claude` | the LLM CLI used for every folk (crew members may override) |
| `OPENFTTH_FOLK_MODEL` | — | model passed to the folk CLI |

`<data dir>/config.json` may hold the same keys (`port`, `engine`, `folkEngine`, `crewPath`,
`maxHops`, `approvalTimeoutMs`).

## How a risky tool gets approved

1. Risky engine tools are not on any folk's allow-list, so the folk's CLI asks its permission
   tool (`mcp__openftth__approve`, served by `src/permission-proxy.ts`).
2. The proxy posts to `/api/internal/permission`; the harness opens an approval card
   (`approval` SSE frame) and holds the request.
3. The user answers via `POST /api/approvals/:id`. On allow, the tool input gains an
   `approval_id`; the design engine verifies it at `/api/internal/approvals/:id` before acting.
