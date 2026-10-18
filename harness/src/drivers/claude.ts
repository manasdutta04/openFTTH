/**
 * Driver for the Claude Code CLI in headless JSON-stream mode.
 *
 * One process per folk stays alive across turns (stream-json in both directions). If it exits,
 * the next turn resumes the same session with `--resume`. Permission prompts for tools that are
 * not on the allow-list go to our MCP permission tool, which turns them into approval cards.
 */

import type { ChildProcess } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { killTree } from "../engine-process.ts";
import { spawnCli } from "./spawn.ts";
import type { Driver, DriverEvent, FolkProcessSpec, TurnResult } from "./types.ts";

interface Pending {
  resolve: (r: TurnResult) => void;
  reject: (e: Error) => void;
  text: string[];
  toolCalls: TurnResult["toolCalls"];
  openTools: Map<string, { tool: string; input: Record<string, unknown> }>;
  interrupted: boolean;
}

export class ClaudeCliDriver implements Driver {
  readonly kind = "claude";
  private child: ChildProcess | null = null;
  private sessionId: string | null = null;
  private pending: Pending | null = null;
  private readonly listeners = new Set<(e: DriverEvent) => void>();
  private disposed = false;
  private readonly spec: FolkProcessSpec;

  constructor(spec: FolkProcessSpec) {
    this.spec = spec;
  }

  get busy(): boolean {
    return this.pending !== null;
  }

  onEvent(cb: (e: DriverEvent) => void): () => void {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  async sendTurn(text: string): Promise<TurnResult> {
    if (this.disposed) throw new Error("driver disposed");
    if (this.pending) throw new Error("folk is busy");
    this.ensureProcess();
    const child = this.child!;
    const result = new Promise<TurnResult>((resolve, reject) => {
      this.pending = { resolve, reject, text: [], toolCalls: [], openTools: new Map(), interrupted: false };
    });
    this.emit({ type: "turn.started" });
    const line = JSON.stringify({ type: "user", message: { role: "user", content: [{ type: "text", text }] } });
    child.stdin!.write(line + "\n");
    return result;
  }

  async interrupt(): Promise<void> {
    if (!this.pending || !this.child) return;
    this.pending.interrupted = true;
    await this.killChild();
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    await this.killChild();
    this.listeners.clear();
  }

  // ---- internals ------------------------------------------------------------------------

  private ensureProcess(): void {
    if (this.child && this.child.exitCode === null) return;
    const s = this.spec;
    // The prompt is long and multi-line; a file keeps it off the command line on every platform.
    const promptPath = join(s.cwd, `folk.${s.folkKey}.system.md`);
    writeFileSync(promptPath, s.systemPrompt, "utf8");
    const args = [
      "-p",
      "--verbose",
      "--output-format",
      "stream-json",
      "--input-format",
      "stream-json",
      "--include-partial-messages",
      "--system-prompt-file",
      promptPath,
      "--mcp-config",
      s.mcpConfigPath,
      "--strict-mcp-config",
      "--permission-prompt-tool",
      s.permissionTool,
      ...(s.allowedTools.length ? ["--allowedTools", s.allowedTools.join(",")] : []),
      ...(s.disallowedTools.length ? ["--disallowedTools", s.disallowedTools.join(",")] : []),
      ...(s.model ? ["--model", s.model] : []),
      ...(this.sessionId ? ["--resume", this.sessionId] : []),
    ];
    // extraArgs come first: they belong to the command itself (e.g. a wrapper script path).
    // On Windows the command is resolved to a file so that no shell rewrites the arguments;
    // a .cmd/.bat launcher still needs cmd.exe, which gets each argument quoted explicitly.
    const child = spawnCli(s.command, [...s.extraArgs, ...args], {
      cwd: s.cwd,
      env: { ...process.env, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1" },
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.child = child;
    const rl = createInterface({ input: child.stdout! });
    rl.on("line", (l) => this.onLine(l));
    child.stderr?.on("data", (d) => s.log(`[${s.folkKey}] ${String(d).trimEnd()}`));
    child.on("exit", (code) => {
      s.log(`[${s.folkKey}] process exited (${code})`);
      this.emit({ type: "exit", code });
      if (this.pending) {
        const p = this.pending;
        this.pending = null;
        const r: TurnResult = { text: p.text.join(""), costUsd: null, usage: null, interrupted: p.interrupted, toolCalls: p.toolCalls };
        if (p.interrupted) {
          this.emit({ type: "turn.completed", text: r.text, costUsd: null, usage: null, interrupted: true });
          p.resolve(r);
        } else {
          this.emit({ type: "error", message: `engine process exited with code ${code}` });
          p.reject(new Error(`engine process exited with code ${code}`));
        }
      }
      if (this.child === child) this.child = null;
    });
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
    const isInit = msg.type === "system" && msg.subtype === "init";
    if (typeof msg.session_id === "string" && (msg.session_id !== this.sessionId || isInit)) {
      this.sessionId = msg.session_id;
      const detail = isInit ? { ...(Array.isArray(msg.tools) ? { tools: msg.tools.map(String) } : {}), ...(typeof msg.claude_code_version === "string" ? { version: msg.claude_code_version } : {}) } : {};
      this.emit({ type: "session", sessionId: msg.session_id, ...detail });
    }
    const p = this.pending;
    switch (msg.type) {
      case "stream_event": {
        const ev = msg.event;
        if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta" && typeof ev.delta.text === "string") {
          this.emit({ type: "text.delta", text: ev.delta.text });
        }
        break;
      }
      case "assistant": {
        for (const block of msg.message?.content ?? []) {
          if (block.type === "text" && typeof block.text === "string") {
            p?.text.push(block.text);
            this.emit({ type: "text", text: block.text });
          } else if (block.type === "tool_use") {
            const input = (block.input ?? {}) as Record<string, unknown>;
            p?.openTools.set(block.id, { tool: block.name, input });
            this.emit({ type: "tool.started", id: block.id, tool: block.name, input });
          }
        }
        break;
      }
      case "user": {
        for (const block of msg.message?.content ?? []) {
          if (block.type === "tool_result") {
            const open = p?.openTools.get(block.tool_use_id);
            const output = normaliseToolOutput(block.content);
            const isError = Boolean(block.is_error);
            if (open) {
              p?.toolCalls.push({ tool: open.tool, input: open.input, output, isError });
              p?.openTools.delete(block.tool_use_id);
            }
            this.emit({ type: "tool.done", id: block.tool_use_id, tool: open?.tool ?? "?", output, isError });
          }
        }
        break;
      }
      case "result": {
        if (!p) break;
        this.pending = null;
        const text = typeof msg.result === "string" && msg.result ? msg.result : p.text.join("");
        const r: TurnResult = {
          text,
          costUsd: typeof msg.total_cost_usd === "number" ? msg.total_cost_usd : null,
          usage: msg.usage ?? null,
          interrupted: false,
          toolCalls: p.toolCalls,
        };
        if (msg.is_error) {
          this.emit({ type: "error", message: text || msg.subtype || "turn failed" });
        }
        this.emit({ type: "turn.completed", text: r.text, costUsd: r.costUsd, usage: r.usage, interrupted: false });
        p.resolve(r);
        break;
      }
      default:
        break;
    }
  }

  private async killChild(): Promise<void> {
    const child = this.child;
    if (!child || child.exitCode !== null) return;
    await new Promise<void>((resolve) => {
      const t = setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, 2000);
      child.once("exit", () => {
        clearTimeout(t);
        resolve();
      });
      killTree(child);
    });
  }

  private emit(e: DriverEvent): void {
    for (const l of this.listeners) l(e);
  }
}

function normaliseToolOutput(content: unknown): unknown {
  if (typeof content === "string") return tryJson(content);
  if (Array.isArray(content)) {
    const text = content.map((c: any) => (typeof c?.text === "string" ? c.text : "")).join("");
    return tryJson(text);
  }
  return content ?? null;
}

function tryJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export const claudeDriverFactory = (spec: FolkProcessSpec): Driver => new ClaudeCliDriver(spec);
