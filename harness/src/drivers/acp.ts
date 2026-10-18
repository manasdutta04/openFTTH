/**
 * Agent Client Protocol driver: JSON-RPC over the agent's stdio. Used for Hermes, and for any
 * other agent that speaks ACP. The agent keeps the session; we send prompts, stream its
 * updates, answer its permission requests, and reach the design engine through MCP servers
 * handed over at session start.
 */

import type { ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { RISKY_ENGINE_TOOLS } from "../approvals.ts";
import { killTree } from "../engine-process.ts";
import { spawnCli } from "./spawn.ts";
import type { Driver, DriverEvent, FolkProcessSpec, TurnResult } from "./types.ts";

const RPC_TIMEOUT_MS = 60_000;

interface Pending {
  text: string[];
  toolCalls: TurnResult["toolCalls"];
  interrupted: boolean;
  usage: Record<string, unknown> | null;
}

interface Waiter {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
}

export class AcpDriver implements Driver {
  readonly kind: string;
  private readonly spec: FolkProcessSpec;
  private readonly args: string[];
  private readonly listeners = new Set<(e: DriverEvent) => void>();
  private child: ChildProcess | null = null;
  private sessionId: string | null = null;
  private nextId = 1;
  private readonly waiting = new Map<number, Waiter>();
  private pending: Pending | null = null;
  private openTools = new Map<string, { tool: string; input: Record<string, unknown> }>();
  private supportsHttpMcp = false;

  constructor(spec: FolkProcessSpec, args: string[]) {
    this.spec = spec;
    this.kind = spec.engine.kind;
    this.args = args;
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
    const first = this.sessionId === null;
    await this.ensureSession();
    const pending: Pending = { text: [], toolCalls: [], interrupted: false, usage: null };
    this.pending = pending;
    this.emit({ type: "turn.started" });
    // ACP has no system prompt: the persona rides on the first prompt of the session.
    const prompt = first ? `${this.spec.systemPrompt}\n\n---\n\n${text}` : text;
    try {
      const result = await this.request("session/prompt", { sessionId: this.sessionId, prompt: [{ type: "text", text: prompt }] }, 0);
      if (result?.usage && typeof result.usage === "object") pending.usage = result.usage as Record<string, unknown>;
      const r: TurnResult = { text: pending.text.join("").trim(), costUsd: null, usage: pending.usage, interrupted: pending.interrupted || result?.stopReason === "cancelled", toolCalls: pending.toolCalls };
      this.emit({ type: "turn.completed", text: r.text, costUsd: null, usage: r.usage, interrupted: r.interrupted });
      return r;
    } catch (err) {
      if (pending.interrupted) {
        const r: TurnResult = { text: pending.text.join("").trim(), costUsd: null, usage: pending.usage, interrupted: true, toolCalls: pending.toolCalls };
        this.emit({ type: "turn.completed", text: r.text, costUsd: null, usage: null, interrupted: true });
        return r;
      }
      this.emit({ type: "error", message: (err as Error).message });
      throw err;
    } finally {
      this.pending = null;
    }
  }

  async interrupt(): Promise<void> {
    if (!this.pending) return;
    this.pending.interrupted = true;
    if (this.sessionId && this.child) this.notify("session/cancel", { sessionId: this.sessionId });
  }

  async dispose(): Promise<void> {
    if (this.child) await killTree(this.child);
    this.child = null;
    this.sessionId = null;
    for (const w of this.waiting.values()) {
      clearTimeout(w.timer);
      w.reject(new Error("driver disposed"));
    }
    this.waiting.clear();
    this.listeners.clear();
  }

  // ---- internals ------------------------------------------------------------------------

  private emit(e: DriverEvent): void {
    for (const cb of this.listeners) cb(e);
  }

  private async ensureSession(): Promise<void> {
    if (this.child && this.child.exitCode === null && this.sessionId) return;
    const s = this.spec;
    const child = spawnCli(s.command, [...s.extraArgs, ...this.args], { cwd: s.cwd, env: { ...process.env }, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    this.sessionId = null;
    const rl = createInterface({ input: child.stdout! });
    rl.on("line", (l) => this.onLine(l));
    child.stderr?.on("data", (d) => s.log(`[${s.folkKey}] ${String(d).trimEnd()}`));
    child.on("exit", (code) => {
      this.emit({ type: "exit", code });
      if (this.child === child) {
        this.child = null;
        this.sessionId = null;
      }
      for (const w of this.waiting.values()) {
        clearTimeout(w.timer);
        w.reject(new Error(`${this.kind} exited with code ${code}`));
      }
      this.waiting.clear();
    });
    const init = await this.request("initialize", { protocolVersion: 1, clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false } });
    this.supportsHttpMcp = Boolean(init?.agentCapabilities?.mcpCapabilities?.http);
    const mcpServers: unknown[] = [
      { name: "openftth", command: s.mcpProxy.command, args: s.mcpProxy.args, env: Object.entries(s.mcpProxy.env).map(([name, value]) => ({ name, value })) },
    ];
    if (this.supportsHttpMcp) mcpServers.push({ type: "http", name: "engine", url: s.engineUrl, headers: [] });
    else s.log(`[${s.folkKey}] ${this.kind} does not accept HTTP MCP servers; the design engine is not reachable from it`);
    const session = await this.request("session/new", { cwd: s.cwd, mcpServers });
    if (typeof session?.sessionId !== "string") throw new Error(`${this.kind}: session/new returned no sessionId`);
    this.sessionId = session.sessionId;
    this.emit({ type: "session", sessionId: session.sessionId, tools: this.supportsHttpMcp ? ["mcp__engine__*", "mcp__openftth__approve"] : ["mcp__openftth__approve"], version: typeof init?.agentInfo?.version === "string" ? `${this.kind} ${init.agentInfo.version}` : this.kind });
  }

  private request(method: string, params: unknown, timeoutMs = RPC_TIMEOUT_MS): Promise<any> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (timeoutMs > 0) {
          this.waiting.delete(id);
          reject(new Error(`${this.kind}: ${method} timed out`));
        }
      }, timeoutMs > 0 ? timeoutMs : 2 ** 31 - 1);
      timer.unref();
      this.waiting.set(id, { resolve, reject, timer });
      this.send({ jsonrpc: "2.0", id, method, params });
    });
  }

  private notify(method: string, params: unknown): void {
    this.send({ jsonrpc: "2.0", method, params });
  }

  private send(msg: unknown): void {
    this.child?.stdin?.write(JSON.stringify(msg) + "\n");
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
    if (typeof msg.id === "number" && !msg.method) {
      const w = this.waiting.get(msg.id);
      if (!w) return;
      this.waiting.delete(msg.id);
      clearTimeout(w.timer);
      if (msg.error) w.reject(new Error(String(msg.error.message ?? "rpc error")));
      else w.resolve(msg.result);
      return;
    }
    if (msg.method === "session/update") {
      this.onUpdate(msg.params?.update ?? {});
      return;
    }
    if (msg.method === "session/request_permission") {
      void this.onPermission(msg.id, msg.params ?? {});
      return;
    }
    if (typeof msg.id === "number" && msg.method) {
      // Any other agent→client request (file reads, terminals): we offered none of those.
      this.send({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: `${msg.method} is not available` } });
    }
  }

  private onUpdate(u: any): void {
    const p = this.pending;
    switch (u.sessionUpdate) {
      case "agent_message_chunk": {
        const text = typeof u.content?.text === "string" ? u.content.text : "";
        if (!text) break;
        p?.text.push(text);
        this.emit({ type: "text.delta", text });
        break;
      }
      case "tool_call": {
        const id = String(u.toolCallId ?? "");
        const tool = toolName(u);
        const input = (u.rawInput ?? {}) as Record<string, unknown>;
        this.openTools.set(id, { tool, input });
        this.emit({ type: "tool.started", id, tool, input });
        break;
      }
      case "tool_call_update": {
        const id = String(u.toolCallId ?? "");
        if (u.status !== "completed" && u.status !== "failed") break;
        const open = this.openTools.get(id) ?? { tool: toolName(u), input: (u.rawInput ?? {}) as Record<string, unknown> };
        this.openTools.delete(id);
        const isError = u.status === "failed";
        const output = u.rawOutput ?? contentText(u.content);
        this.emit({ type: "tool.done", id, tool: open.tool, output, isError });
        p?.toolCalls.push({ tool: open.tool, input: open.input, output, isError });
        break;
      }
      default:
        break;
    }
  }

  /** The agent wants to run something. Engine tools that are risky go through a card; the rest run. */
  private async onPermission(id: number, params: any): Promise<void> {
    const call = params.toolCall ?? {};
    const options: Array<{ optionId: string; kind: string }> = Array.isArray(params.options) ? params.options : [];
    const pick = (prefix: string) => options.find((o) => String(o.kind).startsWith(prefix))?.optionId ?? null;
    const tool = toolName(call);
    const short = tool.replace(/^mcp__[a-z0-9_-]+?__/i, "");
    let allow = true;
    if (RISKY_ENGINE_TOOLS.has(short)) {
      const input = (call.rawInput ?? {}) as Record<string, unknown>;
      if (!input.approval_id) {
        const decision = await this.spec.approve(`mcp__engine__${short}`, input);
        allow = decision.behavior === "allow";
      }
    }
    const optionId = allow ? pick("allow") : pick("reject");
    const result = optionId ? { outcome: { outcome: "selected", optionId } } : { outcome: { outcome: "cancelled" } };
    this.send({ jsonrpc: "2.0", id, result });
  }
}

function toolName(u: any): string {
  const raw = typeof u.rawInput?.tool === "string" ? u.rawInput.tool : null;
  const title = typeof u.title === "string" ? u.title : "tool";
  return raw ?? title.replace(/\s+/g, "_").slice(0, 80);
}

function contentText(content: unknown): unknown {
  if (!Array.isArray(content)) return content ?? null;
  const texts = content.map((c: any) => (typeof c?.content?.text === "string" ? c.content.text : typeof c?.text === "string" ? c.text : null)).filter((t) => t !== null);
  return texts.length === 1 ? texts[0] : texts;
}

export const hermesDriverFactory = (spec: FolkProcessSpec): Driver => new AcpDriver(spec, ["acp"]);
