# Driving openFTTH from other AI apps (MCP)

openFTTH is also an MCP server. Claude Desktop, Claude Code, Cursor or any MCP client can open a
project, import data, design automatically, ask the crew, check, trace, steer and read the
deliverables, through the same harness the app uses. The engine still makes every decision,
and the engineer still approves every risky step in the app.

## Turning it on

In the app: **Crew → Connect other AI apps…** → *Turn on*. The window shows a ready config for
each client with a *Copy* button. The server is off by default.

- It listens on this computer only (loopback), at `/mcp` on the harness, behind its own bearer
  token (`oft_…`), separate from the app's token.
- *New token* revokes the old one at once.
- Settings live in `<dataDir>/mcp.json` (`~/.openftth/mcp.json` for the installed app).

## Connecting

**Claude Desktop and Cursor** start a small stdio bridge (`harness/src/mcp-stdio.ts`, or
`dist/mcp-stdio.js` in a bundle) that forwards to the running app:

```json
{
  "mcpServers": {
    "openftth": {
      "command": "C:\path\to\node.exe",
      "args": ["--no-warnings", "C:\path\to\openFTTH\harness\src\mcp-stdio.ts"],
      "env": { "OPENFTTH_DATA_DIR": "C:\Users\you\.openftth" }
    }
  }
}
```

Claude Desktop reads it from `claude_desktop_config.json` (Settings → Developer → Edit config);
Cursor from `.cursor/mcp.json`. **Claude Code**: `claude mcp add openftth --env
OPENFTTH_DATA_DIR=… -- node --no-warnings …/mcp-stdio.ts`. The window prints all three with your
paths filled in.

The app's harness picks a new port each launch; it writes the current endpoint to `mcp.json`
and the bridge reads it on every connection, so these configs never go stale. The app must be
running.

**Any other client** can use streamable HTTP directly: `POST http://127.0.0.1:<port>/mcp` with
`Authorization: Bearer <token>` (stateless; JSON responses). The port changes per launch.

Web-hosted assistants (for example ChatGPT connectors) need a public HTTPS URL; openFTTH does
not open one. The endpoint refuses requests whose Host or Origin is not loopback.

## Tools

| Tool | What it does |
|---|---|
| `status` | the open project, stage, element counts, crew states, pending approvals, the crew's AI engine and its cost |
| `list_projects`, `list_rulebooks`, `list_import_profiles` | what can be opened |
| `inspect_data` | layers in GIS files and the import profile the engine proposes ([IMPORT.md](IMPORT.md)) |
| `save_import_profile` | save a profile and get the data-quality report |
| `open_project` | an area polygon with an OSM extract, or an import profile |
| `design_automatically` | the whole design with the engine alone (no AI, no cost); returns every step's result |
| `check_design` | design-rule and operator-rule checks |
| `design_summary`, `versions`, `list_assumptions`, `explain_element`, `trace_fibre` | read the design and its recorded reasons |
| `steer` | `move_site`, `exclude_building` or `forbid_street`, with a reason |
| `ask_crew` | a message to the crew in the app; runs the crew's AI engine, which may cost money |
| `list_deliverables` | the deliverable files with their resource URIs |
| `export_for_construction` | **risky**: opens an approval card in the app and waits for the engineer |

Risky engine tools (`add_trench`, `route_outside_ducts`, `rewind_design`, `post_work_order`)
are not offered at all. Everything an outside app does is noted in the project conversation.

## Resources and prompts

- `openftth://project/status` — the project status as JSON.
- `openftth://deliverables/{name}` — BOQ, splice schedule, validation, summary (text), map and
  GeoPackage (binary), up to 20 MB each.
- `openftth://design/{name}` — the design layers as GeoJSON.
- Prompts: `design_area` (area path, optional extract and rulebook), `review_design`,
  `explain_costs`.
