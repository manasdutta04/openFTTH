/**
 * The stdio bridge inside the Claude Desktop bundle (openftth.mcpb): an MCP server on
 * stdin/stdout that forwards every request to openFTTH's MCP link. It reads the link's port and
 * key from `<dataDir>/mcp.json` on every connection, so a new key or a restarted app needs no
 * reinstall. It imports nothing from the harness, so it bundles into one small script.
 *
 * Usage: node mcp-stdio.(ts|js)   (OPENFTTH_DATA_DIR overrides ~/.openftth)
 */

import { readFileSync } from "node:fs";
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

/** The link and whether it is on, from the settings the app writes. */
function readLink(dataDir: string): { enabled: boolean; url: string } {
  let s: Record<string, unknown> = {};
  try {
    s = JSON.parse(readFileSync(join(dataDir, "mcp.json"), "utf8")) as Record<string, unknown>;
  } catch {
    // No settings yet: the link has never been turned on.
  }
  const port = typeof s.listeningPort === "number" ? s.listeningPort : typeof s.port === "number" && s.port > 0 ? s.port : 3920;
  const key = s.requireKey === true && typeof s.key === "string" ? `?key=${s.key}` : "";
  return { enabled: s.enabled === true, url: `http://127.0.0.1:${port}/mcp${key}` };
}

/** Long enough for an automatic design or an approval the engineer answers later. */
const CALL_TIMEOUT_MS = 60 * 60 * 1000;

export function createBridge(dataDir: string): Server {
  let upstream: { client: Client; key: string } | null = null;

  async function connect(): Promise<Client> {
    const s = readLink(dataDir);
    if (!s.enabled) {
      throw new McpError(ErrorCode.InternalError, "openFTTH's MCP link is off. Open openFTTH and turn it on under Connect AI apps.");
    }
    const key = s.url;
    if (upstream?.key === key) return upstream.client;
    await upstream?.client.close().catch(() => {});
    upstream = null;
    const client = new Client({ name: "openftth-stdio-bridge", version: "0.1.0" });
    const transport = new StreamableHTTPClientTransport(new URL(s.url));
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
