/**
 * openFTTH's MCP link: an MCP client over the link and through the Claude Desktop bridge, with
 * the fake design engine. Covers the off switch, the key, loopback-only access, install links,
 * the bundle, the curated tools, resources, prompts and the approval card a risky tool opens.
 */

import { strict as assert } from "node:assert";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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

async function connect(link: string): Promise<Client> {
  const client = new Client({ name: "test-client", version: "0.0.1" });
  await client.connect(new StreamableHTTPClientTransport(new URL(link)));
  return client;
}

const parse = (r: any) => JSON.parse(r.content[0].text);
const link = async (): Promise<any> => (await api("/api/mcp")).json();
const post = async (body: unknown): Promise<any> => (await api("/api/mcp", { method: "POST", body: JSON.stringify(body) })).json();

test("the link is off until the engineer turns it on, then one URL is all a client needs", async () => {
  const off = await link();
  assert.equal(off.enabled, false);
  assert.equal(off.listening, false);
  assert.equal(off.link, "http://127.0.0.1:3920/mcp", "the default link is fixed");

  // Port 0 in tests so a running app's link is never disturbed.
  const on = await post({ enabled: true, port: 0 });
  assert.equal(on.listening, true, on.error);
  assert.match(on.link, /^http:\/\/127\.0\.0\.1:\d+\/mcp$/);
  assert.equal(on.installs.claude_code, `claude mcp add --transport http openftth "${on.link}"`);
  const cursor = new URL(on.installs.cursor);
  assert.equal(cursor.searchParams.get("name"), "openftth");
  assert.deepEqual(JSON.parse(Buffer.from(cursor.searchParams.get("config")!, "base64").toString()), { url: on.link });
  assert.deepEqual(JSON.parse(decodeURIComponent(on.installs.vscode.replace("vscode:mcp/install?", ""))), { name: "openftth", type: "http", url: on.link });

  const client = await connect(on.link);
  assert.ok((await client.listTools()).tools.some((t) => t.name === "design_automatically"));
  await client.close();

  // A web page in a browser cannot reach it.
  const evil = await fetch(on.link, { method: "POST", headers: { origin: "https://evil.example", "content-type": "application/json" }, body: "{}" });
  assert.equal(evil.status, 403);
  // Nor is it on the app's own API port.
  assert.equal((await fetch(`${h.url}/mcp`, { method: "POST" })).status, 404);
});

test("with a key required, the key travels in the link and a new key locks out the old link", async () => {
  const plain = (await link()).link;
  const keyed = await post({ requireKey: true });
  assert.match(keyed.link, /\/mcp\?key=[0-9a-f]{32}$/);
  assert.equal((await fetch(plain, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" })).status, 401);
  const client = await connect(keyed.link);
  assert.equal(parse(await client.callTool({ name: "status", arguments: {} })).open, false);
  await client.close();
  const rotated = await post({ rotate: true });
  assert.notEqual(rotated.link, keyed.link);
  await assert.rejects(connect(keyed.link));
  await post({ requireKey: false });
});

test("tools, resources and prompts are listed; a project opens and its files are readable", async () => {
  const client = await connect((await link()).link);
  const tools = (await client.listTools()).tools.map((t) => t.name);
  for (const t of ["status", "open_project", "inspect_data", "design_automatically", "ask_crew", "check_design", "trace_fibre", "steer", "list_deliverables", "export_for_construction"]) {
    assert.ok(tools.includes(t), `${t} is offered`);
  }
  assert.ok(!tools.includes("add_trench") && !tools.includes("rewind_design"), "risky engine tools are not exposed directly");
  const prompts = (await client.listPrompts()).prompts.map((p) => p.name);
  assert.deepEqual(prompts.sort(), ["design_area", "explain_costs", "review_design"]);

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
  const client = await connect((await link()).link);
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
  assert.equal(parse(await call).approval_id, card.id);

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

test("the Claude Desktop bundle is a valid .mcpb whose bridge follows the link", async () => {
  const { path } = (await (await api("/api/mcp/claude-bundle", { method: "POST" })).json()) as any;
  const zip = readFileSync(path);
  assert.equal(zip.readUInt32LE(0), 0x04034b50, "a ZIP archive");
  const entry = (name: string): Buffer => {
    // The name also appears inside the manifest; the entry is the one after a local header.
    let at = zip.indexOf(Buffer.from(name));
    while (at >= 30 && zip.readUInt32LE(at - 30) !== 0x04034b50) at = zip.indexOf(Buffer.from(name), at + 1);
    const head = at - 30;
    assert.ok(at >= 30, `${name} has a local header`);
    const size = zip.readUInt32LE(head + 18);
    return zip.subarray(at + name.length, at + name.length + size);
  };
  const manifest = JSON.parse(entry("manifest.json").toString("utf8"));
  assert.equal(manifest.server.type, "node");
  assert.equal(manifest.server.entry_point, "server/index.js");
  assert.equal(manifest.server.mcp_config.env.OPENFTTH_DATA_DIR, dataDir);

  // Run the bundled bridge the way Claude Desktop does.
  const dir = mkdtempSync(join(tmpdir(), "openftth-mcpb-"));
  writeFileSync(join(dir, "index.mjs"), entry("server/index.js"));
  const client = new Client({ name: "claude-desktop", version: "0.0.1" });
  await client.connect(new StdioClientTransport({ command: process.execPath, args: [join(dir, "index.mjs")], env: { ...(process.env as Record<string, string>), OPENFTTH_DATA_DIR: dataDir }, stderr: "ignore" }));
  try {
    assert.equal(parse(await client.callTool({ name: "status", arguments: {} })).open, true);
    await post({ enabled: false });
    assert.equal((await link()).listening, false);
    await assert.rejects(client.callTool({ name: "status", arguments: {} }), /turn it on under Connect AI apps/);
  } finally {
    await client.close();
    rmSync(dir, { recursive: true, force: true });
  }
});
