// A stand-in for the Claude Code CLI that speaks its stream-json protocol.
// Behaviour is driven by the user text:
//   contains "handoff"   → replies mentioning @Checker
//   contains "tool"      → emits a tool_use for mcp__engine__design_summary and a result
//   contains "crash"     → exits with code 3 mid-turn
// Everything else echoes the text back.
import { createInterface } from "node:readline";

const sessionId = `fake-${process.pid}`;
const args = process.argv.slice(2);
const resumed = args.includes("--resume");
const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

out({ type: "system", subtype: "init", session_id: sessionId, resumed, tools: ["mcp__engine__design_summary"], model: "fake" });

const rl = createInterface({ input: process.stdin });
rl.on("line", (line) => {
  if (!line.trim()) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const text = (msg.message?.content ?? []).map((c) => c.text ?? "").join("");
  if (/crash/i.test(text)) {
    out({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: "about to crash" }] }, session_id: sessionId });
    process.exit(3);
  }
  if (/tool/i.test(text)) {
    out({ type: "assistant", message: { role: "assistant", content: [{ type: "tool_use", id: "toolu_1", name: "mcp__engine__design_summary", input: {} }] }, session_id: sessionId });
    out({ type: "user", message: { role: "user", content: [{ type: "tool_result", tool_use_id: "toolu_1", content: JSON.stringify({ tool: "design_summary", summary: "3 FATs", version: "abc123def", changes: { added: 1, removed: 0, modified: 0 } }) }] }, session_id: sessionId });
  }
  const reply = /handoff/i.test(text) ? "Twin is built. @Checker please validate." : `echo: ${text}`;
  for (const piece of reply.match(/.{1,12}/g) ?? [reply]) {
    out({ type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: piece } }, session_id: sessionId });
  }
  out({ type: "assistant", message: { role: "assistant", content: [{ type: "text", text: reply }] }, session_id: sessionId });
  out({ type: "result", subtype: "success", is_error: false, result: reply, session_id: sessionId, total_cost_usd: 0.0042, usage: { input_tokens: 10, output_tokens: 5 }, duration_ms: 5 });
});
