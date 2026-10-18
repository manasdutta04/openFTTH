// A stand-in for an ACP agent such as `hermes acp`: JSON-RPC over stdio. On a prompt it
// streams a message chunk, runs one engine tool (asking permission when the prompt says
// "trench"), then returns end_turn.
import { createInterface } from "node:readline";

const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
let nextId = 100;
const waiting = new Map();
const ask = (method, params) =>
  new Promise((resolve) => {
    const id = nextId++;
    waiting.set(id, resolve);
    out({ jsonrpc: "2.0", id, method, params });
  });

const rl = createInterface({ input: process.stdin });
rl.on("line", async (line) => {
  if (!line.trim()) return;
  const msg = JSON.parse(line);
  if (typeof msg.id === "number" && !msg.method) {
    waiting.get(msg.id)?.(msg.result);
    waiting.delete(msg.id);
    return;
  }
  switch (msg.method) {
    case "initialize":
      out({ jsonrpc: "2.0", id: msg.id, result: { protocolVersion: 1, agentInfo: { name: "fake-acp", version: "0.0.1" }, agentCapabilities: { mcpCapabilities: { http: true } } } });
      break;
    case "session/new": {
      const servers = (msg.params.mcpServers ?? []).map((s) => s.name).sort();
      out({ jsonrpc: "2.0", id: msg.id, result: { sessionId: `acp-${servers.join("+")}` } });
      break;
    }
    case "session/prompt": {
      const sessionId = msg.params.sessionId;
      const text = msg.params.prompt.map((p) => p.text ?? "").join("");
      const update = (update) => out({ jsonrpc: "2.0", method: "session/update", params: { sessionId, update } });
      update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Looking. " } });
      if (/trench/i.test(text)) {
        update({ sessionUpdate: "tool_call", toolCallId: "tc1", title: "add_trench", kind: "edit", status: "pending", rawInput: { tool: "mcp__engine__add_trench", surface: "verge" } });
        const answer = await ask("session/request_permission", { sessionId, toolCall: { toolCallId: "tc1", title: "add_trench", rawInput: { tool: "mcp__engine__add_trench", surface: "verge" } }, options: [{ optionId: "ok", kind: "allow_once", name: "Allow" }, { optionId: "no", kind: "reject_once", name: "Reject" }] });
        const allowed = answer?.outcome?.optionId === "ok";
        update({ sessionUpdate: "tool_call_update", toolCallId: "tc1", status: allowed ? "completed" : "failed", rawOutput: allowed ? { dug: true } : { error: "denied" } });
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: allowed ? "Trench added." : "Not allowed." } });
      } else {
        update({ sessionUpdate: "tool_call", toolCallId: "tc0", title: "design_summary", kind: "read", status: "pending", rawInput: { tool: "mcp__engine__design_summary" } });
        update({ sessionUpdate: "tool_call_update", toolCallId: "tc0", status: "completed", rawOutput: { summary: "3 FATs" } });
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: `echo: ${text.split("---").pop().trim()}` } });
      }
      out({ jsonrpc: "2.0", id: msg.id, result: { stopReason: "end_turn" } });
      break;
    }
    case "session/cancel":
      break;
    default:
      if (typeof msg.id === "number") out({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "unknown" } });
  }
});
