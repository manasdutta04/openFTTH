import assert from "node:assert/strict";
import { createServer } from "node:http";
import { join } from "node:path";
import { test } from "node:test";
import { tmpdir } from "node:os";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { OpenAiCompatDriver } from "../src/drivers/openai.ts";
import { CodexCliDriver } from "../src/drivers/codex.ts";
import { AcpDriver } from "../src/drivers/acp.ts";
import { Engines } from "../src/engines.ts";
import type { DriverEvent, FolkProcessSpec } from "../src/drivers/types.ts";

function spec(over: Partial<FolkProcessSpec>): FolkProcessSpec {
  return {
    folkKey: "planner",
    folkName: "Planner",
    projectId: "p",
    systemPrompt: "You are the Planner.",
    allowedTools: ["mcp__engine__design_summary", "mcp__engine__add_trench"],
    disallowedTools: [],
    engineTools: ["design_summary", "add_trench"],
    mcpConfigPath: "unused",
    permissionTool: "mcp__openftth__approve",
    engine: { kind: "openai", model: "test-model", baseUrl: null, apiKey: null },
    cwd: mkdtempSync(join(tmpdir(), "openftth-drv-")),
    command: "",
    extraArgs: [],
    engineUrl: "http://127.0.0.1:1/mcp",
    callEngine: async (tool, args) => ({ tool, args, summary: "3 FATs" }),
    listEngineTools: async () => [
      { name: "design_summary", description: "Summarise", inputSchema: { type: "object", properties: {} } },
      { name: "add_trench", description: "Dig", inputSchema: { type: "object", properties: { surface: { type: "string" } } } },
      { name: "secret_tool", description: "Not for this folk", inputSchema: { type: "object" } },
    ],
    approve: async () => ({ behavior: "allow", updatedInput: { surface: "verge", approval_id: "appr-1" } }),
    mcpProxy: { command: "node", args: [], env: {} },
    log: () => {},
    ...over,
  };
}

/** A fake OpenAI-compatible server: scripted replies, one per request, as SSE. */
function fakeOpenAi(replies: Array<{ text?: string; tool?: { name: string; args: Record<string, unknown> } }>) {
  const requests: any[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    requests.push(body);
    const reply = replies.shift() ?? { text: "no more scripted replies" };
    res.writeHead(200, { "content-type": "text/event-stream" });
    const send = (obj: unknown) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
    if (reply.tool) {
      send({ choices: [{ delta: { tool_calls: [{ index: 0, id: "call_1", function: { name: reply.tool.name, arguments: "" } }] } }] });
      send({ choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: JSON.stringify(reply.tool.args) } }] } }] });
      send({ choices: [{ delta: {}, finish_reason: "tool_calls" }] });
    } else {
      for (const piece of (reply.text ?? "").match(/.{1,6}/g) ?? []) send({ choices: [{ delta: { content: piece } }] });
      send({ choices: [{ delta: {}, finish_reason: "stop" }], usage: { prompt_tokens: 5, completion_tokens: 2 } });
    }
    res.write("data: [DONE]\n\n");
    res.end();
  });
  return new Promise<{ url: string; requests: any[]; close: () => void }>((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const port = (server.address() as { port: number }).port;
      resolve({ url: `http://127.0.0.1:${port}/v1`, requests, close: () => server.close() });
    });
  });
}

test("the OpenAI-compatible driver streams text, runs engine tools and asks approval for risky ones", async () => {
  const srv = await fakeOpenAi([
    { tool: { name: "design_summary", args: {} } },
    { tool: { name: "add_trench", args: { surface: "verge" } } },
    { text: "Done: 3 FATs, trench dug." },
  ]);
  const seen: Record<string, unknown>[] = [];
  const approvals: string[] = [];
  const d = new OpenAiCompatDriver(
    spec({
      engine: { kind: "openai", model: "test-model", baseUrl: srv.url, apiKey: "k" },
      callEngine: async (tool, args) => {
        seen.push({ tool, args });
        return { tool, ok: true };
      },
      approve: async (tool) => {
        approvals.push(tool);
        return { behavior: "allow", updatedInput: { surface: "verge", approval_id: "appr-1" } };
      },
    }),
  );
  const events: DriverEvent[] = [];
  d.onEvent((e) => events.push(e));
  const result = await d.sendTurn("design it");
  srv.close();
  assert.equal(result.text, "Done: 3 FATs, trench dug.");
  assert.deepEqual(seen, [
    { tool: "design_summary", args: {} },
    { tool: "add_trench", args: { surface: "verge", approval_id: "appr-1" } },
  ]);
  assert.deepEqual(approvals, ["mcp__engine__add_trench"]);
  assert.equal(result.toolCalls.length, 2);
  const session = events.find((e) => e.type === "session") as any;
  assert.deepEqual(session.tools, ["design_summary", "add_trench"], "only the folk's tools are offered");
  assert.ok(events.some((e) => e.type === "text.delta"), "text streamed");
  assert.equal(srv.requests[0].tools.length, 2);
  assert.equal(srv.requests[0].messages[0].role, "system");
  assert.equal(srv.requests[2].messages.filter((m: any) => m.role === "tool").length, 2, "tool results went back to the model");
  assert.equal(srv.requests[0].headers, undefined);
});

test("the OpenAI-compatible driver reports a denied risky tool to the model without running it", async () => {
  const srv = await fakeOpenAi([{ tool: { name: "add_trench", args: { surface: "road" } } }, { text: "Understood, not digging." }]);
  let ran = false;
  const d = new OpenAiCompatDriver(
    spec({
      engine: { kind: "ollama", model: "m", baseUrl: srv.url, apiKey: null },
      callEngine: async () => {
        ran = true;
        return {};
      },
      approve: async () => ({ behavior: "deny", message: "Denied by the user: not yet." }),
    }),
  );
  const result = await d.sendTurn("dig");
  srv.close();
  assert.equal(ran, false);
  assert.equal(result.toolCalls[0]!.isError, true);
  const toolMsg = srv.requests[1].messages.find((m: any) => m.role === "tool");
  assert.match(toolMsg.content, /approval_required/);
});

test("the Codex driver runs one exec per turn and resumes the thread", async () => {
  const fake = join(import.meta.dirname, "fake-codex.mjs");
  const d = new CodexCliDriver(spec({ engine: { kind: "codex", model: null, baseUrl: null, apiKey: null }, command: process.execPath, extraArgs: [fake] }));
  const events: DriverEvent[] = [];
  d.onEvent((e) => events.push(e));
  const first = await d.sendTurn("use a tool please");
  assert.equal(first.text, "echo: use a tool please");
  assert.equal(first.toolCalls.length, 1);
  assert.deepEqual(first.toolCalls[0]!.output, { summary: "3 FATs" });
  assert.equal((events.find((e) => e.type === "session") as any).sessionId, "thread-fake-1");
  const second = await d.sendTurn("again");
  assert.equal(second.text, "resumed: echo: again");
  await d.dispose();
});

test("engine settings persist per kind and resolve keys from the environment", async () => {
  const dir = mkdtempSync(join(tmpdir(), "openftth-eng-"));
  const e = new Engines(dir, { kind: "claude", command: "claude" });
  assert.equal(e.current().kind, "claude");
  e.set({ kind: "openrouter", model: "openai/gpt-5", apiKey: "sk-test" });
  assert.equal(e.current().kind, "openrouter");
  assert.equal(e.current().apiKey, "sk-test");
  assert.equal(e.publicCurrent().hasKey, true);
  assert.ok(!("apiKey" in e.publicCurrent()));
  e.set({ kind: "claude" });
  const again = new Engines(dir, { kind: "claude", command: "claude" });
  assert.equal(again.resolved("openrouter").model, "openai/gpt-5", "settings survive a restart");
  assert.equal(again.resolved("openrouter").apiKey, null, "a key never survives in the file");
  assert.ok(!readFileSync(join(dir, "engines.json"), "utf8").includes("sk-test"), "keys are never written to disk");
  assert.throws(() => e.set({ kind: "nope" }));
  const list = await e.list();
  assert.equal(list.length, 6);
  assert.ok(list.every((i) => typeof i.available === "boolean" && typeof i.detail === "string"));
});

test("a key an older version saved in engines.json is handed over once and then removed", () => {
  const dir = mkdtempSync(join(tmpdir(), "openftth-eng-"));
  writeFileSync(join(dir, "engines.json"), JSON.stringify({ current: "openrouter", settings: { openrouter: { model: "m", apiKey: "sk-old" } } }));
  const e = new Engines(dir, { kind: "claude", command: "claude" });
  assert.equal(e.current().apiKey, "sk-old", "it still works this run");
  assert.deepEqual(e.takeLegacyKeys(), { openrouter: "sk-old" });
  assert.ok(!readFileSync(join(dir, "engines.json"), "utf8").includes("sk-old"));
  assert.equal(new Engines(dir, { kind: "claude", command: "claude" }).current().model, "m");
});

test("the ACP driver hands over MCP servers, streams updates and routes risky permissions to a card", async () => {
  const fake = join(import.meta.dirname, "fake-acp.mjs");
  const approvals: string[] = [];
  const d = new AcpDriver(
    spec({
      engine: { kind: "hermes", model: null, baseUrl: null, apiKey: null },
      command: process.execPath,
      extraArgs: [fake],
      approve: async (tool) => {
        approvals.push(tool);
        return { behavior: "allow", updatedInput: { approval_id: "appr-2" } };
      },
    }),
    [],
  );
  const events: DriverEvent[] = [];
  d.onEvent((e) => events.push(e));
  const first = await d.sendTurn("what is there?");
  assert.equal(first.text, "Looking. echo: what is there?");
  assert.equal((events.find((e) => e.type === "session") as any).sessionId, "acp-engine+openftth", "both MCP servers were handed over");
  assert.deepEqual(first.toolCalls[0]!.output, { summary: "3 FATs" });
  const second = await d.sendTurn("dig a trench");
  assert.deepEqual(approvals, ["mcp__engine__add_trench"]);
  assert.equal(second.text, "Looking. Trench added.");
  assert.equal(second.toolCalls[0]!.isError, false);
  const third = await d.sendTurn("run a shell command");
  assert.equal(third.text, "Looking. Shell refused.", "the agent's own shell is refused");
  assert.deepEqual(approvals, ["mcp__engine__add_trench"], "and no card is raised for it");
  await d.dispose();
});
