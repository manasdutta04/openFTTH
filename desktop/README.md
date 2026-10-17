# openFTTH desktop

Rust workspace for the native apps.

| Crate | What |
|---|---|
| `openftth-core` | design bundle reader, rulebook model, Web Mercator helpers; no UI |
| `openftth-client` | typed HTTP + SSE client for the harness protocol |
| `openftth-app` | the Windows app (`openftth` binary): spawns the harness, chat column with approval cards, 2D twin, versions and backstage tabs |

Later crates: `openftth-render` (wgpu scene for the 3D twin), `openftth-voice` (on-device
dictation, call mode), `openftth-ffi` (bindings for the macOS app).

```bash
cd desktop
cargo run -p openftth-app                      # spawns harness/src/index.ts with Node
cargo run -p openftth-app -- --harness-url http://127.0.0.1:8799 --token <token>   # attach
cargo test --workspace
```

The app finds the harness as `harness/dist/index.js` next to the executable (installed layout)
or `harness/src/index.ts` in a checkout; `OPENFTTH_HARNESS` overrides the path and
`OPENFTTH_NODE` the Node executable.
