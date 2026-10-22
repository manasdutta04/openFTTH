/**
 * The stdio bridge other AI apps start (Claude Desktop, Claude Code, Cursor): an MCP server on
 * stdin/stdout that forwards every request to the running openFTTH harness. The harness picks
 * a new port each launch, so the bridge reads the current endpoint and token from
 * `<dataDir>/mcp.json` on every connection and reconnects when the app restarts.
 *
 * Usage: node mcp-stdio.(ts|js)   (OPENFTTH_DATA_DIR overrides ~/.openftth)
 */

import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ErrorCode,
  GetPromptRequestSchema,
  ListPromptsRequestSchema,
  ListResourcesRequestSchema,
  ListResourceTemplatesRequestSchema,
  ListToolsRequestSchema,
  McpError,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { loadMcpSettings } from "./mcp-public.ts";

/** Long enough for an automatic design or an approval the engineer answers later. */
const CALL_TIMEOUT_MS = 60 * 60 * 1000;

export function createBridge(dataDir: string): Server {
  let upstream: { client: Client; key: string } | null = null;

  async function connect(): Promise<Client> {
    const s = loadMcpSettings(dataDir);
    if (!s.enabled || !s.token || !s.url) {
      throw new McpError(ErrorCode.InternalError, "openFTTH's MCP server is off. Open the openFTTH app and turn on Settings → Connect other AI apps.");
    }
    const key = `${s.url}|${s.token}`;
    if (upstream?.key === key) return upstream.client;
    await upstream?.client.close().catch(() => {});
    upstream = null;
    const client = new Client({ name: "openftth-stdio-bridge", version: "0.1.0" });
    const transport = new StreamableHTTPClientTransport(new URL(s.url), { requestInit: { headers: { authorization: `Bearer ${s.token}` } } });
    try {
      await client.connect(transport);
    } catch (err) {
      throw new McpError(ErrorCode.InternalError, `openFTTH is not running (${(err as Error).message}). Open the openFTTH app and try again.`);
    }
    upstream = { client, key };
    return client;
  }

  /** One forwarded request; a dropped connection (the app restarted) is retried once. */
  async function forward<T>(fn: (c: Client) => Promise<T>): Promise<T> {
    try {
      return await fn(await connect());
    } catch (err) {
      if (err instanceof McpError && err.code !== ErrorCode.ConnectionClosed) throw err;
      upstream = null;
      return fn(await connect());
    }
  }

  const server = new Server(
    { name: "openftth", title: "openFTTH", version: "0.1.0" },
    { capabilities: { tools: {}, resources: {}, prompts: {} }, instructions: "openFTTH designs FTTH/GPON fibre networks; start with the `status` tool. Every number comes from the engine's tools: quote them, never estimate." },
  );
  server.setRequestHandler(ListToolsRequestSchema, (req) => forward((c) => c.listTools(req.params)));
  server.setRequestHandler(CallToolRequestSchema, (req) => forward((c) => c.callTool(req.params, undefined, { timeout: CALL_TIMEOUT_MS })));
  server.setRequestHandler(ListResourcesRequestSchema, (req) => forward((c) => c.listResources(req.params)));
  server.setRequestHandler(ListResourceTemplatesRequestSchema, (req) => forward((c) => c.listResourceTemplates(req.params)));
  server.setRequestHandler(ReadResourceRequestSchema, (req) => forward((c) => c.readResource(req.params)));
  server.setRequestHandler(ListPromptsRequestSchema, (req) => forward((c) => c.listPrompts(req.params)));
  server.setRequestHandler(GetPromptRequestSchema, (req) => forward((c) => c.getPrompt(req.params)));
  return server;
}

const isMain = process.argv[1] && import.meta.filename && process.argv[1].replace(/\\/g, "/").endsWith(import.meta.filename.replace(/\\/g, "/").split("/").pop()!);
if (isMain) {
  const dataDir = resolve(process.env.OPENFTTH_DATA_DIR ?? join(homedir(), ".openftth"));
  await createBridge(dataDir).connect(new StdioServerTransport());
}
