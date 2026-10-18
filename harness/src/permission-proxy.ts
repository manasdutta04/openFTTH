/**
 * Permission proxy: a stdio MCP server the folk's engine process spawns. Its single tool,
 * `approve`, forwards a permission prompt to the harness and waits for the user's decision.
 *
 * Env: OPENFTTH_HARNESS_URL, OPENFTTH_COMMS_TOKEN, OPENFTTH_FOLK, OPENFTTH_PROJECT.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const harnessUrl = process.env.OPENFTTH_HARNESS_URL ?? "http://127.0.0.1:8799";
const token = process.env.OPENFTTH_COMMS_TOKEN ?? "";
const folk = process.env.OPENFTTH_FOLK ?? "unknown";
const project = process.env.OPENFTTH_PROJECT ?? "unknown";

const server = new McpServer({ name: "openftth-permissions", version: "0.1.0" });

server.registerTool(
  "approve",
  {
    description: "Ask the user to approve a tool call that is not on this folk's allow-list.",
    inputSchema: {
      tool_name: z.string(),
      input: z.record(z.unknown()).optional(),
      tool_use_id: z.string().optional(),
    },
  },
  async ({ tool_name, input }) => {
    let decision: { behavior: "allow" | "deny"; updatedInput?: Record<string, unknown>; message?: string };
    try {
      // The harness answers only when the card is decided; wait as long as its approval timeout.
      const res = await fetch(`${harnessUrl}/api/internal/permission`, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
        body: JSON.stringify({ folk, project, tool: tool_name, input: input ?? {} }),
        signal: AbortSignal.timeout(Number(process.env.OPENFTTH_APPROVAL_TIMEOUT_MS ?? 20 * 60_000) + 30_000),
      });
      if (!res.ok) {
        decision = { behavior: "deny", message: `harness answered ${res.status}` };
      } else {
        decision = (await res.json()) as typeof decision;
      }
    } catch (err) {
      decision = { behavior: "deny", message: `harness unreachable: ${(err as Error).message}` };
    }
    return { content: [{ type: "text", text: JSON.stringify(decision) }] };
  },
);

await server.connect(new StdioServerTransport());
