// A stand-in for `openftth-engine serve`: a streamable-HTTP MCP server with a few tools.
// Usage: node fake-engine.mjs serve --project <dir> --rulebook <name> --port <port>
import { createServer } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

const argv = process.argv.slice(2);
const port = Number(argv[argv.indexOf("--port") + 1] ?? 0);
const project = argv[argv.indexOf("--project") + 1] ?? "?";

const versions = [{ version: "1111111111111111111111111111111111111111", subject: "[ingest] import_osm: 10 streets", time: 1 }];

function makeServer() {
  const s = new McpServer({ name: "fake-engine", version: "0.0.1" });
  s.registerTool("design_summary", { description: "summary" }, async () => ({ content: [{ type: "text", text: JSON.stringify({ project, stage: "solve", version: versions[0].version, elements: { fat: 3 } }) }] }));
  s.registerTool("versions", { description: "versions", inputSchema: { limit: z.number().optional() } }, async () => ({ content: [{ type: "text", text: JSON.stringify(versions) }] }));
  s.registerTool("diff_versions", { description: "diff", inputSchema: { older: z.string(), newer: z.string() } }, async ({ older, newer }) => ({ content: [{ type: "text", text: JSON.stringify({ added: [older], removed: [], modified: [newer] }) }] }));
  s.registerTool("add_trench", { description: "risky", inputSchema: { approval_id: z.string().optional() } }, async ({ approval_id }) => ({ content: [{ type: "text", text: JSON.stringify(approval_id ? { tool: "add_trench", summary: `trench added under ${approval_id}` } : { error: "approval_required", tool: "add_trench" }) }] }));
  return s;
}

const http = createServer(async (req, res) => {
  if (!req.url?.startsWith("/mcp")) {
    res.writeHead(404).end();
    return;
  }
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  res.on("close", () => void transport.close());
  const server = makeServer();
  await server.connect(transport);
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : undefined;
  await transport.handleRequest(req, res, body);
});
http.listen(port, "127.0.0.1", () => {
  process.stderr.write(`fake engine on ${port}\n`);
});
