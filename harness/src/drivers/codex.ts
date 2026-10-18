/**
 * Codex CLI driver: one `codex exec --json` process per turn, resumed by thread id so the folk
 * keeps its context. The folk's instructions go into AGENTS.md of a private working folder;
 * the sandbox is read-only, so the only way to change the design is the engine's MCP tools.
 * Risky tools need an approval card, which the folk requests through the permission proxy.
 */

import type { ChildProcess } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { killTree } from "../engine-process.ts";
import { spawnCli } from "./spawn.ts";
import type { Driver, DriverEvent, FolkProcessSpec, TurnResult } from "./types.ts";

interface Pending {
  text: string[];
  toolCalls: TurnResult["toolCalls"];
  interrupted: boolean;
  usage: Record<string, unknown> | null;
  resolve: (r: TurnResult) => void;
  reject: (e: Error) => void;
}

export class CodexCliDriver implements Driver {
  readonly kind = "codex";
  private readonly spec: FolkProcessSpec;
  private readonly listeners = new Set<(e: DriverEvent) => void>();
  private child: ChildProcess | null = null;
  private pending: Pending | null = null;
  private threadId: string | null = null;
  private readonly workDir: string;

  constructor(spec: FolkProcessSpec) {
    this.spec = spec;
    this.workDir = join(spec.cwd, "codex", spec.folkKey);
  }

  get busy(): boolean {
    return this.pending !== null;
  }

  onEvent(cb: (e: DriverEvent) => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  async sendTurn(text: string): Promise<TurnResult> {
    if (this.pending) throw new Error("a turn is already running");
    const s = this.spec;
    mkdirSync(this.workDir, { recursive: true });
    writeFileSync(join(this.workDir, "AGENTS.md"), s.systemPrompt, "utf8");
    const config = [
      `mcp_servers.engine.url=${toml(s.engineUrl)}`,
      `mcp_servers.engine.default_tools_approval_mode="approve"`,
      `mcp_servers.openftth.command=${toml(s.mcpProxy.command)}`,
      `mcp_servers.openftth.args=[${s.mcpProxy.args.map(toml).join(", ")}]`,
      `mcp_servers.openftth.env={${Object.entries(s.mcpProxy.env).map(([k, v]) => `${k}=${toml(v)}`).join(", ")}}`,
      `mcp_servers.openftth.default_tools_approval_mode="approve"`,
    ];
    const args = ["exec"];
    if (this.threadId) args.push("resume");
    args.push("--json", "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "-C", this.workDir);
    for (const c of config) args.push("-c", c);
    if (s.engine.model) args.push("-m", s.engine.model);
    if (this.threadId) args.push(this.threadId);
    args.push("-");

    const result = new Promise<TurnResult>((resolve, reject) => {
      this.pending = { text: [], toolCalls: [], interrupted: false, usage: null, resolve, reject };
    });
    this.emit({ type: "turn.started" });
    const child = spawnCli(s.command || "codex", [...s.extraArgs, ...args], {
      cwd: this.workDir,
      env: { ...process.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    const rl = createInterface({ input: child.stdout! });
    rl.on("line", (l) => this.onLine(l));
    child.stderr?.on("data", (d) => {
      const line = String(d).trimEnd();
      if (line && !/Reading additional input from stdin/.test(line)) s.log(`[${s.folkKey}] ${line}`);
    });
    child.on("exit", (code) => {
      this.emit({ type: "exit", code });
      const p = this.pending;
      this.pending = null;
      if (this.child === child) this.child = null;
      if (!p) return;
      const r: TurnResult = { text: p.text.join("\n\n").trim(), costUsd: null, usage: p.usage, interrupted: p.interrupted, toolCalls: p.toolCalls };
      if (code === 0 || p.interrupted) {
        this.emit({ type: "turn.completed", text: r.text, costUsd: null, usage: p.usage, interrupted: p.interrupted });
        p.resolve(r);
      } else {
        this.emit({ type: "error", message: `codex exited with code ${code}` });
        p.reject(new Error(`codex exited with code ${code}`));
      }
    });
    child.stdin!.end(text + "\n");
    return result;
  }

  async interrupt(): Promise<void> {
    if (!this.pending || !this.child) return;
    this.pending.interrupted = true;
    await killTree(this.child);
  }

  async dispose(): Promise<void> {
    if (this.child) await killTree(this.child);
    this.child = null;
    this.listeners.clear();
  }

  // ---- internals ------------------------------------------------------------------------

  private emit(e: DriverEvent): void {
    for (const cb of this.listeners) cb(e);
  }

  private onLine(line: string): void {
    if (!line.trim()) return;
    let msg: any;
    try {
      msg = JSON.parse(line);
    } catch {
      this.spec.log(`[${this.spec.folkKey}] non-JSON: ${line.slice(0, 200)}`);
      return;
    }
    const p = this.pending;
    switch (msg.type) {
      case "thread.started": {
        if (typeof msg.thread_id === "string" && msg.thread_id !== this.threadId) {
          this.threadId = msg.thread_id;
          this.emit({ type: "session", sessionId: msg.thread_id, tools: [`mcp__engine__*`, "mcp__openftth__approve"], version: "codex" });
        }
        break;
      }
      case "item.started": {
        const item = msg.item ?? {};
        if (item.type === "mcp_tool_call") {
          this.emit({ type: "tool.started", id: String(item.id), tool: `mcp__${item.server}__${item.tool}`, input: (item.arguments ?? {}) as Record<string, unknown> });
        } else if (item.type === "command_execution") {
          this.emit({ type: "tool.started", id: String(item.id), tool: "shell", input: { command: item.command ?? "" } });
        }
        break;
      }
      case "item.completed": {
        const item = msg.item ?? {};
        if (item.type === "agent_message" && typeof item.text === "string") {
          p?.text.push(item.text);
          this.emit({ type: "text.delta", text: (p && p.text.length > 1 ? "\n\n" : "") + item.text });
          this.emit({ type: "text", text: item.text });
        } else if (item.type === "mcp_tool_call") {
          const isError = item.status === "failed" || Boolean(item.error);
          const output = isError ? { error: item.error?.message ?? "failed" } : unwrapMcp(item.result);
          const tool = `mcp__${item.server}__${item.tool}`;
          const input = (item.arguments ?? {}) as Record<string, unknown>;
          this.emit({ type: "tool.done", id: String(item.id), tool, output, isError });
          p?.toolCalls.push({ tool, input, output, isError });
        } else if (item.type === "command_execution") {
          const isError = typeof item.exit_code === "number" && item.exit_code !== 0;
          this.emit({ type: "tool.done", id: String(item.id), tool: "shell", output: { output: item.aggregated_output ?? "" }, isError });
        }
        break;
      }
      case "turn.completed": {
        if (p && msg.usage) p.usage = msg.usage as Record<string, unknown>;
        break;
      }
      case "error": {
        this.emit({ type: "error", message: String(msg.message ?? "codex error") });
        break;
      }
      default:
        break;
    }
  }
}

/** The engine answers with one JSON text item per value; show the parsed value when possible. */
function unwrapMcp(result: unknown): unknown {
  const content = (result as { content?: Array<{ type: string; text?: string }> } | null)?.content;
  if (!Array.isArray(content)) return result;
  const texts = content.filter((c) => c.type === "text").map((c) => c.text ?? "");
  const parse = (t: string) => {
    try {
      return JSON.parse(t);
    } catch {
      return t;
    }
  };
  if (texts.length === 1) return parse(texts[0]!);
  return texts.map(parse);
}

function toml(s: string): string {
  return JSON.stringify(s);
}

export const codexDriverFactory = (spec: FolkProcessSpec): Driver => new CodexCliDriver(spec);
