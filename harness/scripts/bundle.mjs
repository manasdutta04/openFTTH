// Bundle the harness into dist/ (three entry points: the server, the permission proxy and the MCP stdio bridge).
import { build } from "esbuild";
import { rmSync } from "node:fs";

rmSync("dist", { recursive: true, force: true });
await build({
  entryPoints: ["src/index.ts", "src/permission-proxy.ts", "src/mcp-stdio.ts"],
  outdir: "dist",
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node24",
  sourcemap: true,
  logLevel: "info",
  banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
});
