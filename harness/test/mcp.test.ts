/**
 * The public MCP server: an MCP client over streamable HTTP and through the stdio bridge, with
 * the fake design engine. Covers the off switch, the token, the curated tools, resources,
 * prompts and the approval card a risky tool opens in the app.
 */

import { strict as assert } from "node:assert";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { findRepoRoot } from "../src/config.ts";
import { claudeDriverFactory } from "../src/drivers/claude.ts";
import { freePort } from "../src/engine-process.ts";
import { startHarness, type Harness } from "../src/index.ts";

const repo = findRepoRoot();
const TOKEN = "test-app-token";
let dataDir: string;
let fakeEngine: ChildProcess;
let h: Harness;

before(async () => {
  dataDir = mkdtempSync(join(tmpdir(), "openftth-mcp-test-"));
  const port = await freePort();
  fakeEngine = spawn(process.execPath, [join(import.meta.dirname, "fake-engine.mjs"), "serve", "--project", dataDir, "--rulebook", "x", "--port", String(port)], { stdio: ["ignore", "ignore", "pipe"] });
  await new Promise<void>((resolve) => fakeEngine.stderr!.once("data", () => resolve()));
  h = await startHarness({
    cfg: { dataDir, port: 0, appToken: TOKEN, commsToken: "test-comms-token", approvalTimeoutMs: 10_000, maxHops: 3, folkEngine: { kind: "claude", command: process.execPath, args: [join(import.meta.dirname, "fake-claude.mjs")] } },
    driverFactory: claudeDriverFactory,
    engineAttachUrl: `http://127.0.0.1:${port}/mcp`,
    log: () => {},
  });
});

after(async () => {
  await h.close();
  fakeEngine.kill();
  rmSync(dataDir, { recursive: true, force: true });
});

const api = (path: string, init: RequestInit = {}) =>
  fetch(`${h.url}${path}`, { ...init, headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json", ...(init.headers ?? {}) } });

async function connect(token: string): Promise<Client> {
  const client = new Client({ name: "test-client", version: "0.0.1" });
  await client.connect(new StreamableHTTPClientTransport(new URL(`${h.url}/mcp`), { requestInit: { headers: { authorization: `Bearer ${token}` } } }));
  return client;
}

const parse = (r: any) => JSON.parse(r.content[0].text);

test("the MCP server is off until the engineer turns it on, and needs its own token", async () => {
  const off = await fetch(`${h.url}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal(off.status, 404);
  assert.match(((await off.json()) as any).error, /Connect other AI apps/);

  const on: any = await (await api("/api/mcp", { method: "POST", body: JSON.stringify({ enabled: true }) })).json();
  assert.equal(on.enabled, true);
  assert.match(on.token, /^oft_[0-9a-f]{48}$/);
  assert.equal(on.url, `${h.url}/mcp`);
  assert.match(on.snippets.claude_desktop, /mcp-stdio/);
  assert.match(on.snippets.claude_code, /^claude mcp add openftth/);

  const noToken = await fetch(`${h.url}/mcp`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal(noToken.status, 401);
  await assert.rejects(connect(TOKEN), "the app token is not the MCP token");
});

test("tools, resources and prompts are listed; a project opens and its files are readable", async () => {
  const info: any = await (await api("/api/mcp")).json();
  const client = await connect(info.token);
  const tools = (await client.listTools()).tools.map((t) => t.name);
  for (const t of ["status", "open_project", "inspect_data", "design_automatically", "ask_crew", "check_design", "trace_fibre", "steer", "list_deliverables", "export_for_construction"]) {
    assert.ok(tools.includes(t), `${t} is offered`);
  }
  assert.ok(!tools.includes("add_trench") && !tools.includes("rewind_design"), "risky engine tools are not exposed directly");
  const prompts = (await client.listPrompts()).prompts.map((p) => p.name);
  assert.deepEqual(prompts.sort(), ["design_area", "explain_costs", "review_design"]);

  const closed = parse(await client.callTool({ name: "status", arguments: {} }));
  assert.equal(closed.open, false);
  const noProject = await client.callTool({ name: "list_deliverables", arguments: {} });
  assert.equal(noProject.isError, true);

  const meta = parse(await client.callTool({ name: "open_project", arguments: { area_path: join(repo, "fixtures", "gent", "area.geojson"), rulebook: "generic-gpon" } }));
  assert.match(meta.id, /^area-/);
  const status = parse(await client.callTool({ name: "status", arguments: {} }));
  assert.equal(status.open, true);
  assert.equal(status.crew_engine.cost, "subscription");
  const trace = parse(await client.callTool({ name: "trace_fibre", arguments: { element_uuid: "home-1" } }));
  assert.equal(trace.data.olt_port, 3);

  mkdirSync(join(meta.dir, "deliverables"), { recursive: true });
  writeFileSync(join(meta.dir, "deliverables", "boq.csv"), "item,qty\nfat,3\n");
  writeFileSync(join(meta.dir, "deliverables", "map.png"), Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  const listed = parse(await client.callTool({ name: "list_deliverables", arguments: {} }));
  assert.deepEqual(listed.map((f: any) => f.uri), ["openftth://deliverables/boq.csv", "openftth://deliverables/map.png"]);
  const resources = (await client.listResources()).resources.map((r) => r.uri);
  assert.ok(resources.includes("openftth://deliverables/boq.csv") && resources.includes("openftth://project/status"));
  const csv = await client.readResource({ uri: "openftth://deliverables/boq.csv" });
  assert.equal((csv.contents[0] as any).text, "item,qty\nfat,3\n");
  const png = await client.readResource({ uri: "openftth://deliverables/map.png" });
  assert.equal((png.contents[0] as any).blob, "iVBORw==");
  await assert.rejects(client.readResource({ uri: "openftth://deliverables/..%2Fharness.json" }));

  const prompt = await client.getPrompt({ name: "design_area", arguments: { area_path: "C:/a.geojson" } });
  assert.match((prompt.messages[0]!.content as any).text, /open_project, then design_automatically/);
  await client.close();
});

test("a risky export waits for the engineer's card in the app", async () => {
  const info: any = await (await api("/api/mcp")).json();
  const client = await connect(info.token);
  const call = client.callTool({ name: "export_for_construction", arguments: { why: "handover" } });
  let card: any;
  for (let i = 0; i < 50 && !card; i++) {
    await new Promise((r) => setTimeout(r, 50));
    card = ((await (await api("/api/approvals")).json()) as any[]).find((a) => a.state === "pending" && a.tool === "export_for_construction");
  }
  assert.ok(card, "an approval card opened in the app");
  assert.equal(card.folk, "external");
  assert.match(card.summary, /Another AI app asks.*handover/);
  await api(`/api/approvals/${card.id}`, { method: "POST", body: JSON.stringify({ decision: "allow" }) });
  const out = parse(await call);
  assert.equal(out.approval_id, card.id);

  const denied = client.callTool({ name: "export_for_construction", arguments: {} });
  let second: any;
  for (let i = 0; i < 50 && !second; i++) {
    await new Promise((r) => setTimeout(r, 50));
    second = ((await (await api("/api/approvals")).json()) as any[]).find((a) => a.state === "pending");
  }
  await api(`/api/approvals/${second.id}`, { method: "POST", body: JSON.stringify({ decision: "deny", note: "not yet" }) });
  const res = await denied;
  assert.equal(res.isError, true);
  assert.match((res.content as any)[0].text, /not yet/);
  await client.close();
});

test("the stdio bridge forwards to the running harness and follows a new token", async () => {
  const bridge = (env: Record<string, string>) =>
    new StdioClientTransport({ command: process.execPath, args: ["--no-warnings", join(import.meta.dirname, "..", "src", "mcp-stdio.ts")], env: { ...(process.env as Record<string, string>), ...env }, stderr: "ignore" });
  const client = new Client({ name: "desktop-app", version: "0.0.1" });
  await client.connect(bridge({ OPENFTTH_DATA_DIR: dataDir }));
  assert.ok((await client.listTools()).tools.some((t) => t.name === "design_automatically"));
  assert.equal(parse(await client.callTool({ name: "status", arguments: {} })).open, true);

  await api("/api/mcp", { method: "POST", body: JSON.stringify({ rotate: true }) });
  assert.equal(parse(await client.callTool({ name: "status", arguments: {} })).open, true, "the bridge reads the new token");

  await api("/api/mcp", { method: "POST", body: JSON.stringify({ enabled: false }) });
  await assert.rejects(client.callTool({ name: "status", arguments: {} }), /turn on Settings → Connect other AI apps/);
  await client.close();
});
