# openFTTH — guide for coding agents

Read `docs/PLAN.md` first. It holds the architecture, the decisions already taken and the
roadmap. Do not re-open those decisions unless the user asks.

## Non-negotiable rules

1. **The LLM never writes numbers into the design.** Every design mutation is an engine
   tool call that takes ids and rule references. Solvers and validators produce numbers
   and record a `reason` for each decision.
2. **Risky tools require approval** (`route_outside_ducts`, `add_trench`,
   `export_for_construction`, `rewind_design`). They are never on an allow-list and the
   engine refuses them without a valid `approval_id`.
3. **Determinism.** Same inputs → same element ids and same output. CP-SAT runs with a fixed
   worker count, `interleave_search=True` and a fixed seed. Any randomness takes a fixed
   seed; `engine/tests/test_pipeline_gent.py` guards byte-identical bundles.
4. **Licences.** MIT code only. All code in this repository is written for it; do not paste
   code from other projects.
5. **Local first.** Prefer local files (`.osm`, GeoPackage) over network fetches; network
   fetches are explicit, named tools.

## Layout and commands

- `engine/` — Python 3.12, managed with **uv**. `cd engine && uv sync && uv run pytest`.
  CLI: `uv run openftth-engine --help`. MCP server: `uv run openftth-engine serve`.
- `harness/` — Node 24, pnpm. `cd harness && pnpm install && pnpm test && pnpm dev`.
- `rulebooks/` — YAML presets; `schema.json` is generated from the pydantic model
  (`uv run openftth-engine schema --out ../rulebooks/schema.json`). Never hand-edit it.
- `crews/openftth/` — crew manifest + folk instructions.
- `fixtures/gent/` — committed small sample only; large downloads go to `fixtures/**/raw/`
  (git-ignored).
- `desktop/` — Rust workspace (`cargo test --workspace`, `cargo clippy -D warnings`).
- `macos/` — later; see the plan.

## Conventions

- Commits: imperative subject, body explains *why*. One feature per commit. No co-author
  trailers. Never push without being asked.
- Python: `ruff format` + `ruff check` (line length 100, E501 ignored for long decision
  strings), type hints, pydantic v2 models for all on-disk formats.
- TypeScript: strict, ESM, Node's built-in test runner, no framework for the HTTP server.
- Every engine tool returns `{summary, changes, decisions, warnings}` and commits to the
  project repo through the single-writer queue.
- Tests live next to the code they test. Property tests guard the solver invariants listed
  in the plan.
