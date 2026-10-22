/**
 * End-to-end tests of the HTTP API with a fake folk engine (speaks the CLI protocol) and a
 * fake design engine (an MCP server). Nothing here needs network access or an LLM.
 */

import { strict as assert } from "node:assert";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { findRepoRoot } from "../src/config.ts";
import { claudeDriverFactory } from "../src/drivers/claude.ts";
import { freePort } from "../src/engine-process.ts";
import { parseSse } from "../src/events.ts";
import { startHarness, type Harness } from "../src/index.ts";

const repo = findRepoRoot();
const TOKEN = "test-app-token";
let dataDir: string;
let fakeEngine: ChildProcess;
let fakeEngineUrl: string;
let h: Harness;

before(async () => {
  dataDir = mkdtempSync(join(tmpdir(), "openftth-harness-test-"));
  const port = await freePort();
  fakeEngine = spawn(process.execPath, [join(import.meta.dirname, "fake-engine.mjs"), "serve", "--project", dataDir, "--rulebook", "x", "--port", String(port)], { stdio: ["ignore", "ignore", "pipe"] });
  await new Promise<void>((resolve) => fakeEngine.stderr!.once("data", () => resolve()));
  fakeEngineUrl = `http://127.0.0.1:${port}/mcp`;
  h = await startHarness({
    cfg: {
      dataDir,
      port: 0,
      appToken: TOKEN,
      commsToken: "test-comms-token",
      approvalTimeoutMs: 5_000,
      maxHops: 3,
      folkEngine: { kind: "claude", command: process.execPath, args: [join(import.meta.dirname, "fake-claude.mjs")] },
      engine: { command: [process.execPath, join(import.meta.dirname, "fake-engine.mjs")], cwd: import.meta.dirname, rulebook: "generic-gpon" },
    },
    driverFactory: claudeDriverFactory,
    engineAttachUrl: fakeEngineUrl,
    log: () => {},
  });
});

after(async () => {
  await h.close();
  fakeEngine.kill();
  rmSync(dataDir, { recursive: true, force: true });
});

const api = (path: string, init: RequestInit = {}, token = TOKEN) =>
  fetch(`${h.url}${path}`, { ...init, headers: { authorization: `Bearer ${token}`, "content-type": "application/json", ...(init.headers ?? {}) } });
const body = async (r: Response | Promise<Response>): Promise<any> => (await r).json();

test("health needs no token; everything else does", async () => {
  const health = await fetch(`${h.url}/api/health`);
  assert.equal(health.status, 200);
  assert.equal((await body(health)).ok, true);
  const noToken = await fetch(`${h.url}/api/crew`);
  assert.equal(noToken.status, 401);
  const badToken = await api("/api/crew", {}, "wrong");
  assert.equal(badToken.status, 401);
  const ok = await api("/api/crew");
  assert.equal(ok.status, 200);
  assert.equal((await body(ok)).members.length, 4);
});

test("non-loopback Origin is refused", async () => {
  const res = await api("/api/crew", { headers: { origin: "https://evil.example" } });
  assert.equal(res.status, 403);
});

test("opening a project attaches the engine and answers status, versions and diff", async () => {
  const res = await api("/api/project", { method: "POST", body: JSON.stringify({ area_path: join(repo, "fixtures", "gent", "area.geojson"), rulebook: "generic-gpon" }) });
  const text = await res.text();
  assert.equal(res.status, 201, text);
  const meta = JSON.parse(text);
  assert.match(meta.id, /^area-[0-9a-f]{6}$/);
  const status = await body(api("/api/project"));
  assert.equal(status.open, true);
  assert.equal(status.summary.elements.fat, 3);
  const versions = await body(api("/api/project/versions"));
  assert.equal(versions.length, 1);
  const diff = await body(api("/api/project/diff?from=a&to=b"));
  assert.deepEqual(diff.added, ["a"]);
  const approved = await body(api("/api/project/approve", { method: "POST", body: JSON.stringify({ version: versions[0].version }) }));
  assert.deepEqual(approved.approvedVersions, [versions[0].version]);
});

test("rulebooks, projects, element explanation and assumptions are served", async () => {
  const rulebooks = await body(api("/api/rulebooks"));
  assert.ok(rulebooks.map((r: any) => r.name).includes("belgium"));
  assert.ok(rulebooks.find((r: any) => r.name === "norway").country === "NO");
  const projects = await body(api("/api/projects"));
  assert.equal(projects.length, 1);
  assert.match(projects[0].id, /^area-/);
  const explained = await body(api("/api/project/element/abc"));
  assert.equal(explained.props.name, "FAT-001");
  const assumptions = await body(api("/api/project/assumptions"));
  assert.equal(assumptions.length, 1);
});

test("a user turn runs the default responder and streams frames", async () => {
  const frames: Array<{ event?: string; data: any }> = [];
  const off = h.bus.on((f) => frames.push({ event: f.kind, data: f.data }));
  const res = await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "Hello planner, tool please", designContext: { stage: "solve", selection: ["uuid-1"] }, wait: true }) });
  assert.equal(res.status, 200);
  off();
  const kinds = frames.map((f) => f.event);
  assert.ok(kinds.includes("message.patch"), "text deltas streamed");
  assert.ok(kinds.includes("tool.activity"), "tool chips emitted");
  assert.ok(kinds.includes("design.version"), "engine tool result with a version emits design.version");
  const stages = frames.filter((f) => f.event === "design.stage").map((f) => f.data.stage);
  assert.deepEqual(stages, ["solve"], "the engine's stage follows the version, once per change");
  const folkStates = frames.filter((f) => f.event === "folk").map((f) => f.data.state);
  assert.ok(folkStates.includes("thinking") && folkStates.includes("idle"));
  const messages = await body(api("/api/room/messages"));
  const reply = messages.find((m: any) => m.role === "folk" && m.kind === "text" && m.folk === "planner");
  assert.ok(reply, "planner replied");
  assert.match(reply.text, /^echo: \[design context: stage=solve; selection=uuid-1\]/);
  const toolMsg = messages.find((m: any) => m.kind === "tool");
  assert.equal(toolMsg.text, "3 FATs");
});

test("a reply that mentions a teammate hands the turn over", async () => {
  await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "@Surveyor build the twin then handoff", wait: true }) });
  const messages = await body(api("/api/room/messages"));
  const folks = messages.filter((m: any) => m.role === "folk" && m.kind === "text").map((m: any) => m.folk);
  assert.ok(folks.includes("surveyor") && folks.includes("checker"), `expected surveyor then checker, got ${folks.join(",")}`);
  const handoff = messages.find((m: any) => m.kind === "handoff");
  assert.equal(handoff.text, "Surveyor → Checker");
  const checkerReply = messages.filter((m: any) => m.folk === "checker" && m.kind === "text").pop();
  assert.match(checkerReply.text, /\[from Surveyor\]/);
});

test("a permission prompt becomes an approval card; allowing a risky tool injects approval_id", async () => {
  const frames: any[] = [];
  const off = h.bus.on((f) => f.kind === "approval" && frames.push(f.data));
  const pending = fetch(`${h.url}/api/internal/permission`, { method: "POST", headers: { authorization: "Bearer test-comms-token", "content-type": "application/json" }, body: JSON.stringify({ folk: "planner", project: "p", tool: "mcp__engine__add_trench", input: { surface: "verge" } }) });
  await new Promise((r) => setTimeout(r, 100));
  const list = await body(api("/api/approvals"));
  const card = list.find((a: any) => a.state === "pending");
  assert.ok(card, "card is pending");
  assert.equal(card.shortTool, "add_trench");
  const decided = await api(`/api/approvals/${card.id}`, { method: "POST", body: JSON.stringify({ decision: "allow", note: "ok" }) });
  assert.equal(decided.status, 200);
  const decision = await body(pending);
  assert.equal(decision.behavior, "allow");
  assert.equal(decision.updatedInput.approval_id, card.id);
  assert.equal(decision.updatedInput.surface, "verge");
  const check = await body(fetch(`${h.url}/api/internal/approvals/${card.id}`, { headers: { authorization: "Bearer test-comms-token" } }));
  assert.deepEqual(check, { approved: true, tool: "add_trench", state: "allowed" });
  off();
  assert.deepEqual(frames.map((f) => f.state), ["pending", "allowed"]);
  const wrongToken = await fetch(`${h.url}/api/internal/approvals/${card.id}`, { headers: { authorization: `Bearer ${TOKEN}` } });
  assert.equal(wrongToken.status, 401, "the app token must not open internal routes");
});

test("denying an approval resolves the prompt with deny", async () => {
  const pending = fetch(`${h.url}/api/internal/permission`, { method: "POST", headers: { authorization: "Bearer test-comms-token", "content-type": "application/json" }, body: JSON.stringify({ folk: "drafter", project: "p", tool: "mcp__engine__export_for_construction", input: {} }) });
  await new Promise((r) => setTimeout(r, 100));
  const card = (await body(api("/api/approvals"))).find((a: any) => a.state === "pending");
  await api(`/api/approvals/${card.id}`, { method: "POST", body: JSON.stringify({ decision: "deny", note: "not yet" }) });
  const decision = await body(pending);
  assert.equal(decision.behavior, "deny");
  assert.match(decision.message, /not yet/);
});

test("built-in tools on the folk deny list are refused without a card", async () => {
  const decision = await body(fetch(`${h.url}/api/internal/permission`, { method: "POST", headers: { authorization: "Bearer test-comms-token", "content-type": "application/json" }, body: JSON.stringify({ folk: "planner", project: "p", tool: "Bash", input: { command: "ls" } }) }));
  assert.equal(decision.behavior, "deny");
  assert.match(decision.message, /engine tools/);
  const list = await body(api("/api/approvals"));
  assert.ok(!list.some((a: any) => a.tool === "Bash"), "no card was opened");
});

test("purging a project deletes its folder, conversation and runtime log", async () => {
  const meta = await body(api("/api/project", { method: "POST", body: JSON.stringify({ area_path: join(repo, "fixtures", "gent", "area.geojson"), rulebook: "generic-gpon" }) }));
  await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "hello" }) });
  await new Promise((r) => setTimeout(r, 300));
  assert.ok((await body(api("/api/room/messages"))).length > 0);
  assert.ok((await body(api("/api/projects"))).some((p: any) => p.id === meta.id));
  const res = await api("/api/project?purge=1", { method: "DELETE" });
  assert.equal(res.status, 200);
  assert.ok(!(await body(api("/api/projects"))).some((p: any) => p.id === meta.id), "project folder is gone");
  const reopened = await body(api("/api/project", { method: "POST", body: JSON.stringify({ area_path: join(repo, "fixtures", "gent", "area.geojson"), rulebook: "generic-gpon" }) }));
  assert.equal(reopened.id, meta.id);
  assert.equal((await body(api("/api/room/messages"))).length, 0, "conversation starts empty");
  assert.equal((await body(api("/api/backstage"))).length, 0, "runtime log starts empty");
});

test("a chain stops when a folk repeats itself instead of looping to the hop limit", async () => {
  const before = (await body(api("/api/room/messages"))).length;
  await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "@Surveyor you are stuck", wait: true }) });
  const messages = (await body(api("/api/room/messages"))).slice(before);
  const stop = messages.filter((m: any) => m.kind === "handoff" && /waiting for you/.test(m.text));
  assert.equal(stop.length, 1, "the room explains why it stopped");
  assert.ok(!messages.some((m: any) => /hand-off limit/.test(m.text)), "the hop limit was never needed");
});

test("only risky engine tools open a card; teammates' tools and reads are answered at once", async () => {
  const ask = (tool: string, folk = "planner") => body(fetch(`${h.url}/api/internal/permission`, { method: "POST", headers: { authorization: "Bearer test-comms-token", "content-type": "application/json" }, body: JSON.stringify({ folk, project: "p", tool, input: {} }) }));
  const read = await ask("Glob");
  assert.equal(read.behavior, "allow");
  const other = await ask("mcp__engine__import_osm");
  assert.equal(other.behavior, "deny");
  assert.match(other.message, /@Surveyor/);
  const unknown = await ask("SomeOtherTool");
  assert.equal(unknown.behavior, "deny");
  const cards = await body(api("/api/approvals"));
  assert.ok(!cards.some((a: any) => ["Glob", "mcp__engine__import_osm", "SomeOtherTool"].includes(a.tool)), "no card was opened for any of them");
});

test("engines are listed with availability and can be switched", async () => {
  const listed = await body(api("/api/engines"));
  assert.equal(listed.current.kind, "claude");
  assert.equal(listed.engines.length, 6);
  assert.ok(listed.engines.every((e: any) => typeof e.available === "boolean" && typeof e.label === "string"));
  assert.ok(!listed.engines.some((e: any) => "apiKey" in e), "keys never leave the harness");
  const frames: any[] = [];
  const off = h.bus.on((f) => f.kind === "engine" && frames.push(f.data));
  const switched = await body(api("/api/engine", { method: "POST", body: JSON.stringify({ kind: "openrouter", model: "openai/gpt-5", apiKey: "sk-test" }) }));
  assert.equal(switched.kind, "openrouter");
  assert.equal(switched.hasKey, true);
  assert.equal(frames.length, 1);
  off();
  const bad = await api("/api/engine", { method: "POST", body: JSON.stringify({ kind: "nope" }) });
  assert.equal(bad.status, 400);
  const back = await body(api("/api/engine", { method: "POST", body: JSON.stringify({ kind: "claude" }) }));
  assert.equal(back.kind, "claude");
});

test("the engineer's own change goes straight to the engine, tells the room and queues the Checker", async () => {
  const frames: Array<{ event?: string; data: any }> = [];
  const off = h.bus.on((f) => frames.push({ event: f.kind, data: f.data }));
  const res = await api("/api/project/steer", { method: "POST", body: JSON.stringify({ tool: "move_site", args: { element_uuid: "fdh-1", lon: 3.7, lat: 51.0, reason: "public side of the fence" } }) });
  const out = await body(res);
  assert.equal(res.status, 200, JSON.stringify(out));
  assert.equal(out.tool, "move_site");
  assert.match(out.version, /^2+$/);
  // The Checker's turn is queued; a turn sent behind it with wait=true returns once both ran.
  await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "ping", wait: true }) });
  off();
  assert.ok(frames.some((f) => f.event === "design.version" && f.data.tool === "move_site"), "the app is told to reload the design");
  const messages = await body(api("/api/room/messages"));
  const note = messages.find((m: any) => m.kind === "steer");
  assert.ok(note, "the room records the engineer's move");
  assert.equal(note.role, "user");
  assert.match(note.text, /^move_site done — public side of the fence$/);
  assert.deepEqual(note.json.reran, ["route_feeder", "route_drops"]);
  const ask = messages.find((m: any) => m.role === "user" && m.kind === "text" && m.text.startsWith("@Checker"));
  assert.ok(ask, "the Checker is asked to validate again");
  assert.match(ask.text, /moved a site by hand .*reason: public side of the fence.*re-ran route_feeder, route_drops/);
  const reply = messages.filter((m: any) => m.folk === "checker" && m.kind === "text").pop();
  assert.ok(reply && /moved a site by hand/.test(reply.text), "the Checker got the turn");

  // A rejection by the engine, an unknown tool and a missing reason never reach the room.
  const rejected = await api("/api/project/steer", { method: "POST", body: JSON.stringify({ tool: "move_site", args: { element_uuid: "fdh-1", lon: 3.7, lat: 51.0, reason: "  " }, revalidate: false }) });
  assert.equal(rejected.status, 422);
  assert.match((await body(rejected)).error, /^a reason is required/);
  const unknown = await api("/api/project/steer", { method: "POST", body: JSON.stringify({ tool: "import_osm", args: {} }) });
  assert.equal(unknown.status, 400);
  const quiet = await api("/api/project/steer", { method: "POST", body: JSON.stringify({ tool: "forbid_street", args: { osm_id: "w12", reason: "fresh asphalt" }, revalidate: false }) });
  assert.equal(quiet.status, 200);
  const later = await body(api("/api/room/messages"));
  assert.equal(later.filter((m: any) => m.kind === "steer").length, 2);
  assert.equal(later.filter((m: any) => m.role === "user" && m.text.startsWith("@Checker")).length, 1, "revalidate=false asks nobody");
});

test("a home's fibre trace is served straight from the engine", async () => {
  const out = await body(api("/api/project/trace/home-1"));
  assert.equal(out.data.reaches_olt, true);
  assert.equal(out.data.olt_port, 3);
  assert.equal(out.data.steps[0].side, "OLT");
});

test("posting a work order is a risky tool that opens a card", async () => {
  const { RISKY_ENGINE_TOOLS } = await import("../src/approvals.ts");
  assert.ok(RISKY_ENGINE_TOOLS.has("post_work_order"));
});

test("engines say what they cost, and Ollama is free", async () => {
  const listed = await body(api("/api/engines"));
  const byKind = Object.fromEntries(listed.engines.map((e: any) => [e.kind, e]));
  assert.equal(byKind.ollama.cost, "free");
  assert.equal(byKind.claude.cost, "subscription");
  assert.ok(listed.engines.every((e: any) => typeof e.costNote === "string" && e.costNote.length > 0));
});

test("automatic design needs a map extract", async () => {
  const res = await api("/api/project/autodesign", { method: "POST" });
  assert.equal(res.status, 400);
  assert.match((await body(res)).error, /map extract/);
});

test("import: files are inspected, a profile is saved and checked, and a project opens from it", async () => {
  const area = join(repo, "fixtures", "gent", "area.geojson");
  const inspected = await body(api("/api/import/inspect", { method: "POST", body: JSON.stringify({ paths: [area, "C:/data/adressen.csv"] }) }));
  assert.equal(inspected.layers.length, 2);
  assert.deepEqual(inspected.profile.bindings.map((b: any) => b.role), ["area", "addresses"]);
  assert.equal(inspected.roles.area.label, "Design area");

  const bad = await api("/api/import/profile", { method: "POST", body: JSON.stringify({ name: "no area", profile: { ...inspected.profile, bindings: [] } }) });
  assert.equal(bad.status, 422);
  assert.match((await body(bad)).error, /no design area/);

  const saved = await body(api("/api/import/profile", { method: "POST", body: JSON.stringify({ name: "Lambert test", profile: inspected.profile }) }));
  assert.equal(saved.report.level, "ok");
  assert.match(saved.path, /Lambert-test\.yaml$/);
  assert.equal(saved.report.profile, "Lambert-test");
  const listed = await body(api("/api/import/profiles"));
  assert.ok(listed.some((p: any) => p.name === "Lambert-test"));

  // Reopening from Recent names the profile as the area: it is still a profile project.
  const res = await api("/api/project", { method: "POST", body: JSON.stringify({ area_path: saved.path, rulebook: "generic-gpon" }) });
  const meta = await body(res);
  assert.equal(res.status, 201, JSON.stringify(meta));
  assert.equal(meta.profilePath, saved.path);
  const crew = await body(api("/api/crew"));
  assert.ok(crew.members.find((m: any) => m.key === "surveyor").tools.includes("import_data"));
  // Back to the classic project the other tests use.
  await api("/api/project", { method: "POST", body: JSON.stringify({ area_path: join(repo, "fixtures", "gent", "area.geojson"), rulebook: "generic-gpon" }) });
});

test("tool activity says where a tool acts, and only that", async () => {
  const { toolTarget } = await import("../src/room.ts");
  assert.deepEqual(toolTarget({ element_uuid: "e1", reason: "x", api: "secret" }), { target: { element_uuid: "e1" } });
  assert.deepEqual(toolTarget({ lon: 3.7, lat: 51.0, osm_id: "w1" }), { target: { lon: 3.7, lat: 51.0, osm_id: "w1" } });
  assert.deepEqual(toolTarget({ time_limit_s: 20 }), {});
  assert.deepEqual(toolTarget(null), {});
});

test("SSE stream replays missed frames on resume", async () => {
  const first = h.bus.emit("runtime", { marker: 1 });
  h.bus.emit("runtime", { marker: 2 });
  const ac = new AbortController();
  const res = await fetch(`${h.url}/api/events`, { headers: { authorization: `Bearer ${TOKEN}`, "last-event-id": first.id }, signal: ac.signal });
  const reader = res.body!.getReader();
  const { value } = await reader.read();
  ac.abort();
  const frames = parseSse(new TextDecoder().decode(value));
  assert.equal(frames[0]!.event, "hello");
  assert.equal((frames[0]!.data as any).resumed, true);
  assert.ok(frames.some((f) => f.event === "runtime" && (f.data as any).marker === 2), "frame after the cursor replayed");
  assert.ok(!frames.some((f) => f.event === "runtime" && (f.data as any).marker === 1), "frame at the cursor not repeated");
});

test("a crashing folk process reports an error and the next turn resumes the session", async () => {
  const res = await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "@Drafter please crash now", wait: true }) });
  assert.equal(res.status, 200);
  const messages = await body(api("/api/room/messages"));
  assert.ok(messages.some((m: any) => m.kind === "error" && /exited with code 3/.test(m.text)));
  const again = await api("/api/room/messages", { method: "POST", body: JSON.stringify({ text: "@Drafter are you back?", wait: true }) });
  assert.equal(again.status, 200);
  const later = await body(api("/api/room/messages"));
  const reply = later.filter((m: any) => m.folk === "drafter" && m.kind === "text").pop();
  assert.match(reply.text, /are you back/);
  const backstage = await body(api("/api/backstage"));
  assert.ok(backstage.some((e: any) => e.event === "session"));
});
