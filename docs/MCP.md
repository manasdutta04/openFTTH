# Connecting AI apps (MCP)

openFTTH has an MCP server, like other desktop apps that ship one. You turn it on, and any MCP
client can use openFTTH through **one link**:

```
http://127.0.0.1:3920/mcp
```

Claude Desktop, Claude Code, Cursor, VS Code or any other MCP client can then open a project,
import data, design automatically, check, trace, steer, ask the crew and pull the deliverables.
The engine still makes every decision, and the engineer still approves every risky step in the
app.

## Turning it on

In the app: **Connect AI apps** on the start page, in the Crew or Help menu, or the
*MCP link* indicator in the status bar. Flip the switch on. The link is the same every time
openFTTH starts, so an app added once keeps working.

## Adding it to an app

| App | How |
|---|---|
| Claude Desktop | *Install*: openFTTH writes `openftth.mcpb` (an MCP Bundle) and opens it; Claude Desktop shows its install prompt |
| Claude Code | *Copy* gives one command: `claude mcp add --transport http openftth "http://127.0.0.1:3920/mcp"` |
| Cursor | *Open* follows Cursor's install link; Cursor asks to add the server |
| VS Code | *Open* follows VS Code's install link (`vscode:mcp/install`); VS Code asks to add the server |
| Anything else | paste the link where the app asks for an MCP server URL (Streamable HTTP) |

The Claude Desktop bundle runs a small bridge that reads the link from openFTTH's settings
every time it connects, so a new key or another port needs no reinstall. openFTTH must be
running for any app to reach it.

## Who can use the link

- It answers on this computer only (127.0.0.1). Requests whose Host or Origin is not local are
  refused, so a web page in a browser cannot drive it.
- **Require a key** adds `?key=…` to the link: still one link to paste, and requests without it
  are refused. *New key* locks out every app that has the old link.
- Risky engine tools (`add_trench`, `route_outside_ducts`, `rewind_design`, `post_work_order`)
  are not offered. `export_for_construction` is, but it opens an approval card in the app and
  waits for the engineer. Everything an outside app does is noted in the project conversation.
- ChatGPT and other web-based assistants connect to MCP servers over the internet; they cannot
  reach a link on this computer.

Settings live in `<dataDir>/mcp.json` (`~/.openftth/mcp.json` for the installed app):
`enabled`, `port` (default 3920), `requireKey`, `key`.

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

## Resources and prompts

- `openftth://project/status`: the project status as JSON.
- `openftth://deliverables/{name}`: BOQ, splice schedule, validation, summary (text), map and
  GeoPackage (binary), up to 20 MB each.
- `openftth://design/{name}`: the design layers as GeoJSON.
- Prompts: `design_area` (area path, optional extract and rulebook), `review_design`,
  `explain_costs`.
