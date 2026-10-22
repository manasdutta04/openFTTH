/**
 * openFTTH's MCP link: one fixed local address, `http://127.0.0.1:3920/mcp`, that any MCP client
 * can add, like other desktop apps that ship an MCP server. The harness's own API moves to a new
 * port each launch; this link does not, so a client set up once keeps working.
 *
 * It is off until the engineer turns it on, answers loopback only (Host and Origin are checked,
 * so a web page cannot reach it), and can require a key carried in the link itself. One-click
 * installs are offered for Claude Desktop (an .mcpb bundle), Claude Code, Cursor and VS Code.
 */

import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { join } from "node:path";
import { crc32 } from "node:zlib";
import { handleMcpRequest, type PublicMcpDeps } from "./mcp-public.ts";
import { readJsonOr, writeJsonAtomic } from "./store.ts";

export const DEFAULT_MCP_PORT = 3920;

export interface McpSettings {
  enabled: boolean;
  port: number;
  /** When set, the link carries `?key=…` and requests without it are refused. */
  requireKey: boolean;
  key: string;
}

const settingsPath = (dataDir: string) => join(dataDir, "mcp.json");

export function loadMcpSettings(dataDir: string): McpSettings {
  const s = readJsonOr<Partial<McpSettings>>(settingsPath(dataDir), {});
  return {
    enabled: s.enabled === true,
    port: Number.isInteger(s.port) && (s.port as number) >= 0 ? (s.port as number) : DEFAULT_MCP_PORT,
    requireKey: s.requireKey === true,
    key: typeof s.key === "string" && s.key ? s.key : newKey(),
  };
}

export function updateMcpSettings(dataDir: string, change: { enabled?: boolean; requireKey?: boolean; rotate?: boolean; port?: number }): McpSettings {
  const s = loadMcpSettings(dataDir);
  if (typeof change.enabled === "boolean") s.enabled = change.enabled;
  if (typeof change.requireKey === "boolean") s.requireKey = change.requireKey;
  if (Number.isInteger(change.port) && change.port! >= 0 && change.port! < 65536) s.port = change.port!;
  if (change.rotate) s.key = newKey();
  const { listeningPort } = readJsonOr<{ listeningPort?: number | null }>(settingsPath(dataDir), {});
  writeJsonAtomic(settingsPath(dataDir), { ...s, listeningPort: listeningPort ?? null });
  return s;
}

function newKey(): string {
  return randomBytes(16).toString("hex");
}

/** The link a client adds, with the key in it when one is required. */
export function mcpLink(port: number, s: Pick<McpSettings, "requireKey" | "key">): string {
  return `http://127.0.0.1:${port}/mcp${s.requireKey ? `?key=${s.key}` : ""}`;
}

/** One-click installs: deeplinks that open each app's own "add server" prompt. */
export function installLinks(link: string): Record<string, string> {
  return {
    claude_code: `claude mcp add --transport http openftth "${link}"`,
    cursor: `cursor://anysphere.cursor-deeplink/mcp/install?name=openftth&config=${Buffer.from(JSON.stringify({ url: link })).toString("base64")}`,
    vscode: `vscode:mcp/install?${encodeURIComponent(JSON.stringify({ name: "openftth", type: "http", url: link }))}`,
  };
}

/** The fixed-port listener; `apply()` brings it in line with the saved settings. */
export class McpLink {
  private server: Server | null = null;
  private port: number | null = null;
  private error: string | null = null;
  private readonly deps: PublicMcpDeps;

  constructor(deps: PublicMcpDeps) {
    this.deps = deps;
  }

  async apply(): Promise<void> {
    const s = loadMcpSettings(this.deps.cfg.dataDir);
    const wanted = s.enabled ? s.port : null;
    if (this.server && (wanted === null || (wanted !== 0 && wanted !== this.port))) await this.close();
    this.error = null;
    if (wanted === null || this.server) return;
    const server = createServer((req, res) => void this.handle(req, res));
    try {
      await new Promise<void>((resolve, reject) => {
        server.once("error", reject);
        server.listen(wanted, "127.0.0.1", () => resolve());
      });
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      this.error = code === "EADDRINUSE" ? `Port ${wanted} is already used by another program.` : (err as Error).message;
      return;
    }
    server.requestTimeout = 0;
    server.headersTimeout = 0;
    const addr = server.address();
    this.port = typeof addr === "object" && addr ? addr.port : wanted;
    this.server = server;
    this.recordPort(this.port);
  }

  async close(): Promise<void> {
    const s = this.server;
    this.server = null;
    this.port = null;
    this.recordPort(null);
    if (s) {
      s.closeAllConnections();
      await new Promise<void>((resolve) => s.close(() => resolve()));
    }
  }

  /** The port the link is on now (it differs from the setting only for port 0), for the bridge. */
  private recordPort(port: number | null): void {
    const path = settingsPath(this.deps.cfg.dataDir);
    const s = readJsonOr<Record<string, unknown>>(path, {});
    if ((s.listeningPort ?? null) === port) return;
    writeJsonAtomic(path, { ...s, listeningPort: port });
  }

  /** What the app's Connect window shows. */
  info(): Record<string, unknown> {
    const s = loadMcpSettings(this.deps.cfg.dataDir);
    const port = this.port ?? s.port;
    const link = mcpLink(port, s);
    return {
      enabled: s.enabled,
      listening: this.server !== null,
      error: this.error,
      port,
      link,
      requireKey: s.requireKey,
      installs: installLinks(link),
    };
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const s = loadMcpSettings(this.deps.cfg.dataDir);
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    const host = (req.headers.host ?? "").replace(/:\d+$/, "");
    const origin = req.headers.origin;
    if (!["127.0.0.1", "localhost", "[::1]"].includes(host) || (origin && !/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(origin))) {
      reply(res, 403, { error: "openFTTH's MCP link answers this computer only" });
      return;
    }
    if (url.pathname !== "/mcp") {
      reply(res, url.pathname === "/" ? 200 : 404, { name: "openFTTH", mcp: mcpLink(this.port ?? s.port, { requireKey: false, key: "" }) });
      return;
    }
    if (s.requireKey) {
      const given = url.searchParams.get("key") ?? (req.headers.authorization ?? "").replace(/^Bearer\s+/i, "");
      if (given !== s.key) {
        reply(res, 401, { error: "this link needs its key; copy the link again from openFTTH → Connect AI apps" });
        return;
      }
    }
    try {
      const body = req.method === "POST" ? await readJson(req) : undefined;
      await handleMcpRequest(this.deps, req, res, body);
    } catch (err) {
      if (!res.headersSent) reply(res, 400, { error: (err as Error).message });
    }
  }
}

function reply(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(text) });
  res.end(text);
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  if (!chunks.length) return undefined;
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new Error("invalid JSON body");
  }
}

// ---- Claude Desktop: an MCP Bundle (.mcpb) wrapping the stdio bridge ---------------------------

/**
 * Write `<dataDir>/openftth.mcpb`. Claude Desktop installs it with a double-click. The bundle
 * runs the stdio bridge, which reads the current link (and key) from this data dir each time it
 * connects, so turning the key on or issuing a new one needs no reinstall.
 */
export async function writeClaudeBundle(dataDir: string, version: string): Promise<string> {
  const manifest = {
    manifest_version: "0.3",
    name: "openftth",
    display_name: "openFTTH",
    version,
    description: "Design FTTH fibre networks with openFTTH: open projects, design automatically, check, trace and read the deliverables.",
    author: { name: "openFTTH" },
    license: "MIT",
    server: {
      type: "node",
      entry_point: "server/index.js",
      mcp_config: { command: "node", args: ["${__dirname}/server/index.js"], env: { OPENFTTH_DATA_DIR: dataDir } },
    },
  };
  const path = join(dataDir, "openftth.mcpb");
  writeFileSync(path, zip([
    { name: "manifest.json", data: Buffer.from(JSON.stringify(manifest, null, 2)) },
    { name: "server/index.js", data: Buffer.from(await bridgeScript()) },
  ]));
  return path;
}

/** The stdio bridge as one self-contained script: the bundled one, or built now in a checkout. */
async function bridgeScript(): Promise<string> {
  const built = join(import.meta.dirname, "mcp-stdio.js");
  if (existsSync(built)) return readFileSync(built, "utf8");
  const esbuild = await import("esbuild");
  const out = await esbuild.build({
    entryPoints: [join(import.meta.dirname, "mcp-stdio.ts")],
    bundle: true,
    platform: "node",
    format: "esm",
    target: "node20",
    write: false,
    banner: { js: "import { createRequire } from 'node:module'; const require = createRequire(import.meta.url);" },
  });
  return out.outputFiles[0]!.text;
}

/** A ZIP archive with stored (uncompressed) entries: all an .mcpb needs. */
function zip(entries: Array<{ name: string; data: Buffer }>): Buffer {
  const local: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;
  for (const e of entries) {
    const name = Buffer.from(e.name, "utf8");
    const crc = crc32(e.data) >>> 0;
    const head = Buffer.alloc(30);
    head.writeUInt32LE(0x04034b50, 0);
    head.writeUInt16LE(20, 4);
    head.writeUInt16LE(0x0800, 6); // UTF-8 names
    head.writeUInt16LE(0, 8); // stored
    head.writeUInt32LE(0x00210000, 10); // 1980-01-01 00:00: fixed, so the bundle is reproducible
    head.writeUInt32LE(crc, 14);
    head.writeUInt32LE(e.data.length, 18);
    head.writeUInt32LE(e.data.length, 22);
    head.writeUInt16LE(name.length, 26);
    head.writeUInt16LE(0, 28);
    local.push(head, name, e.data);
    const dir = Buffer.alloc(46);
    dir.writeUInt32LE(0x02014b50, 0);
    dir.writeUInt16LE(20, 4);
    dir.writeUInt16LE(20, 6);
    dir.writeUInt16LE(0x0800, 8);
    dir.writeUInt16LE(0, 10);
    dir.writeUInt32LE(0x00210000, 12);
    dir.writeUInt32LE(crc, 16);
    dir.writeUInt32LE(e.data.length, 20);
    dir.writeUInt32LE(e.data.length, 24);
    dir.writeUInt16LE(name.length, 28);
    dir.writeUInt32LE(offset, 42);
    central.push(dir, name);
    offset += head.length + name.length + e.data.length;
  }
  const size = central.reduce((n, b) => n + b.length, 0);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(size, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, ...central, end]);
}
