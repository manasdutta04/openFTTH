# Security policy

openFTTH is a desktop app for one person on their own Windows PC. It designs fibre networks
from local files, and it can talk to an AI engine you choose. This page says what it protects,
how, and how to report a problem.

## Supported versions

Security fixes go into the newest release only. Check Help → Check for updates, or the
[releases page](https://github.com/manasdutta04/openFTTH/releases).

## Reporting a vulnerability

Please report privately through
[GitHub Security Advisories](https://github.com/manasdutta04/openFTTH/security/advisories/new)
("Report a vulnerability"). Do not open a public issue for a security problem.

- You get an acknowledgement within 5 working days.
- A first assessment follows within 10 working days.
- A fix for a confirmed issue ships as soon as it is ready, normally within 30 days, with
  credit to you if you want it.

## What runs where

- **The app** (`openftth.exe`) starts two helpers on your PC: the harness (Node) and the design
  engine (Python). Both listen on `127.0.0.1` only, on ports chosen at random per run.
- **Every call to the harness needs a random token** that the app creates at each start and
  never writes to disk. Requests from a web page (a browser `Origin` that is not loopback) or
  for a host name other than loopback are refused, which stops DNS-rebinding attacks.
  Comparisons of tokens and keys take constant time, and request bodies are capped at 8 MB.
- **The MCP link for other AI apps** (`http://127.0.0.1:3920/mcp`) is off until you turn it on in
  Connect AI apps. It listens on loopback only and can require a key, which you can rotate.
- **Nothing is sent anywhere unless you ask.** There is no telemetry. Network calls happen only
  for the AI engine you choose, for an explicit map download (the engine's `fetch_osm` tool, which
  the crew runs only when asked) and for Help → Check for updates.

## Your AI keys

Keys for OpenRouter or an OpenAI-compatible API are stored in **Windows Credential Manager**,
encrypted for your Windows account. The harness keeps them in memory only and never writes them
to a file. Keys an older version saved in `engines.json` are moved to Credential Manager at the
first start and removed from the file. A key can also come from an environment variable
(`OPENROUTER_API_KEY`). The Claude and Codex CLIs use their own sign-in, outside openFTTH.

## What the AI crew may do

The crew changes the design only through the engine's own tools, each logged with a reason.

- **Shell and file tools are denied** to every crew member (Claude CLI: `--disallowedTools`;
  Codex: `--sandbox read-only`; agents over ACP such as Hermes: only openFTTH's own tools are
  allowed, every other permission request is refused; local and API models only ever see the
  engine's tools).
- **Risky tools never run without you:** posting a work order, digging a new trench, routing
  outside ducts, exporting for construction and rewinding the design each open an approval card.
  The engine itself refuses them without a valid approval from that card.
- While the crew works, manual editing is locked, so you and the crew never change the map at
  the same time.

## Your data

Projects, settings and logs stay in `%USERPROFILE%\.openftth`. Each project is a local git
history of versions. Uninstalling asks before deleting this folder. Files you import are read
where they are; openFTTH reads only the files you choose.

## Known limits

- The installer is not code-signed yet, so Windows SmartScreen may warn about an unknown
  publisher. Check the download against the `.sha256` file published with each release.
- Another program running as your Windows user could read your projects, or call the app's
  local ports if it got the run's token. openFTTH does not defend against malware on your
  own account.
- Import files are parsed by GDAL (through pyogrio). Open data only from sources you trust.

## How we keep dependencies safe

`tools/check.ps1 -Audit` (and the manual Security workflow) audits the Rust (`cargo audit`),
Node (`pnpm audit`) and Python (`pip-audit`) dependency trees for known vulnerabilities before
each release; dependencies are updated by hand, and
lock files pin every version.
