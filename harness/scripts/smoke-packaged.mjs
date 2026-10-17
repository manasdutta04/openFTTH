// Run the bundled harness from a temp dir (no node_modules) and check /api/health.
import { spawn } from "node:child_process";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const tmp = mkdtempSync(join(tmpdir(), "openftth-harness-"));
cpSync(resolve("dist"), join(tmp, "dist"), { recursive: true });
const child = spawn(process.execPath, ["--no-warnings", join(tmp, "dist", "index.js")], {
  env: { ...process.env, OPENFTTH_APP_TOKEN: "smoke", OPENFTTH_PORT: "0", OPENFTTH_DATA_DIR: join(tmp, "data"), OPENFTTH_CREW: resolve("..", "crews", "openftth", "crew.openftth.json") },
  stdio: ["ignore", "inherit", "pipe"],
});
let url = null;
child.stderr.on("data", (d) => {
  const m = String(d).match(/listening on (http:\/\/127\.0\.0\.1:\d+)/);
  if (m) url = m[1];
  process.stderr.write(d);
});
const deadline = Date.now() + 15000;
while (!url && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
if (!url) {
  child.kill();
  rmSync(tmp, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  throw new Error("harness did not start");
}
const res = await fetch(`${url}/api/health`);
const body = await res.json();
const exited = new Promise((r) => child.once("exit", r));
child.kill();
await exited;
rmSync(tmp, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
if (!body.ok) throw new Error(`health failed: ${JSON.stringify(body)}`);
console.log("packaged harness OK:", body);
