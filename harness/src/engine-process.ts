/**
 * The design engine as a child process: one `openftth-engine serve` per open project,
 * reachable over streamable-HTTP MCP, plus a small MCP client the harness uses itself for
 * versions, diffs and summaries.
 */

import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { EngineConfig } from "./config.ts";

export interface EngineHandle {
  url: string;
  port: number;
  pid: number | null;
  call<T = unknown>(tool: string, args?: Record<string, unknown>): Promise<T>;
  listTools(): Promise<string[]>;
  /** Tool names, descriptions and input schemas, for drivers that call tools themselves. */
  listToolDefs(): Promise<Array<{ name: string; description: string; inputSchema: Record<string, unknown> }>>;
  stop(): Promise<void>;
  onExit(cb: (code: number | null) => void): void;
}

export interface StartOptions {
  projectDir: string;
  approvalsUrl: string;
  commsToken: string;
  log?: (line: string) => void;
  startTimeoutMs?: number;
  /** For tests: a prebuilt URL of an engine that is already running. */
  attachUrl?: string;
}

export async function startEngine(cfg: EngineConfig, opts: StartOptions): Promise<EngineHandle> {
  if (opts.attachUrl) return attach(opts.attachUrl);
  const port = await freePort();
  const [cmd, ...baseArgs] = cfg.command;
  if (!cmd) throw new Error("engine command is empty");
  const args = [...baseArgs, "serve", "--project", opts.projectDir, "--rulebook", cfg.rulebook, "--port", String(port)];
  const child = spawn(cmd, args, {
    cwd: cfg.cwd,
    env: { ...process.env, OPENFTTH_APPROVALS_URL: opts.approvalsUrl, OPENFTTH_COMMS_TOKEN: opts.commsToken, PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
    shell: process.platform === "win32" && !cmd.endsWith(".exe"),
    windowsHide: true,
  });
  const log = opts.log ?? (() => {});
  child.stdout?.on("data", (d) => log(String(d).trimEnd()));
  child.stderr?.on("data", (d) => log(String(d).trimEnd()));
  const url = `http://127.0.0.1:${port}/mcp`;
  const handle = await waitForEngine(url, child, opts.startTimeoutMs ?? 60_000);
  return handle;
}

async function attach(url: string): Promise<EngineHandle> {
  const port = Number(new URL(url).port);
  return makeHandle(url, port, null);
}

async function waitForEngine(url: string, child: ChildProcess, timeoutMs: number): Promise<EngineHandle> {
  const deadline = Date.now() + timeoutMs;
  let exited: number | null | undefined;
  child.on("exit", (code) => {
    exited = code;
  });
  while (Date.now() < deadline) {
    if (exited !== undefined) throw new Error(`engine exited with code ${exited} before it was ready`);
    try {
      const h = await makeHandle(url, Number(new URL(url).port), child);
      return h;
    } catch {
      await sleep(400);
    }
  }
  child.kill();
  throw new Error(`engine did not answer on ${url} within ${timeoutMs} ms`);
}

async function makeHandle(url: string, port: number, child: ChildProcess | null): Promise<EngineHandle> {
  // Probe once with a fresh client so a failed connect does not leave a half-open session.
  const probe = await connect(url);
  await probe.close();
  const exitCbs: Array<(code: number | null) => void> = [];
  child?.on("exit", (code) => exitCbs.forEach((cb) => cb(code)));
  return {
    url,
    port,
    pid: child?.pid ?? null,
    async call<T>(tool: string, args: Record<string, unknown> = {}): Promise<T> {
      const client = await connect(url);
      try {
        // Solver steps on a large area can run for minutes; the SDK default is 60 s.
        const res = await client.callTool({ name: tool, arguments: args }, undefined, { timeout: 15 * 60_000, resetTimeoutOnProgress: true });
        const content = (res as { content?: Array<{ type: string; text?: string }> }).content ?? [];
        // A list result arrives as one text item per element; a dict as a single JSON text.
        const texts = content.filter((c) => c.type === "text").map((c) => c.text ?? "");
        if (texts.length === 0) return null as unknown as T;
        if (texts.length === 1) return parseLoose(texts[0]!) as T;
        return texts.map(parseLoose) as unknown as T;
      } finally {
        await client.close();
      }
    },
    async listTools(): Promise<string[]> {
      const client = await connect(url);
      try {
        const res = await client.listTools();
        return res.tools.map((t) => t.name).sort();
      } finally {
        await client.close();
      }
    },
    async listToolDefs() {
      const client = await connect(url);
      try {
        const res = await client.listTools();
        return res.tools.map((t) => ({ name: t.name, description: t.description ?? "", inputSchema: (t.inputSchema ?? { type: "object" }) as Record<string, unknown> })).sort((a, b) => a.name.localeCompare(b.name));
      } finally {
        await client.close();
      }
    },
    async stop(): Promise<void> {
      if (!child || child.exitCode !== null) return;
      await new Promise<void>((resolve) => {
        const t = setTimeout(() => {
          child.kill("SIGKILL");
          resolve();
        }, 3000);
        child.once("exit", () => {
          clearTimeout(t);
          resolve();
        });
        killTree(child);
      });
    },
    onExit(cb) {
      exitCbs.push(cb);
    },
  };
}

async function connect(url: string): Promise<Client> {
  const client = new Client({ name: "openftth-harness", version: "0.1.0" });
  const transport = new StreamableHTTPClientTransport(new URL(url));
  await client.connect(transport);
  return client;
}

function parseLoose(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** Kill a child and, on Windows (where a shell wrapper owns the real process), its whole tree. */
export function killTree(child: ChildProcess): void {
  if (process.platform === "win32" && child.pid) {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore", windowsHide: true }).on("error", () => child.kill());
  } else {
    child.kill();
  }
}

export function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
