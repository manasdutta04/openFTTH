// A stand-in for the Codex CLI's `exec --json` mode. Prints its argument list as a runtime
// line (so tests can check the resume id), one MCP tool call and a message, then exits 0.
const args = process.argv.slice(2);
const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");
let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => (input += d));
process.stdin.on("end", () => {
  const resumed = args.includes("resume");
  out({ type: "thread.started", thread_id: resumed ? args[args.indexOf("resume") + 1 + args.slice(args.indexOf("resume") + 1).findIndex((a) => /^[0-9a-f-]{8,}$/.test(a))] : "thread-fake-1" });
  out({ type: "turn.started" });
  if (/tool/i.test(input)) {
    out({ type: "item.started", item: { id: "item_0", type: "mcp_tool_call", server: "engine", tool: "design_summary", arguments: {}, result: null, error: null, status: "in_progress" } });
    out({ type: "item.completed", item: { id: "item_0", type: "mcp_tool_call", server: "engine", tool: "design_summary", arguments: {}, result: { content: [{ type: "text", text: JSON.stringify({ summary: "3 FATs" }) }] }, error: null, status: "completed" } });
  }
  out({ type: "item.completed", item: { id: "item_1", type: "agent_message", text: `${resumed ? "resumed: " : ""}echo: ${input.trim()}` } });
  out({ type: "turn.completed", usage: { input_tokens: 10, output_tokens: 3 } });
  process.exit(0);
});
