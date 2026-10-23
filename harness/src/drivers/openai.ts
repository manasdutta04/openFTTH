/**
 * OpenAI-compatible chat driver: Ollama, OpenRouter, LM Studio, vLLM, OpenAI itself. The
 * driver keeps the conversation, streams the reply, and runs the tool loop itself: engine
 * tools are called over MCP, and a risky tool opens an approval card before it runs.
 */

import { randomUUID } from "node:crypto";
import { RISKY_ENGINE_TOOLS } from "../approvals.ts";
import type { Driver, DriverEvent, EngineToolDef, FolkProcessSpec, TurnResult } from "./types.ts";

const MAX_ROUNDS = 24;

interface ChatMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

interface ToolCall {
  id: string;
  type: "function";
  function: { name: string; arguments: string };
}

interface Pending {
  text: string[];
  toolCalls: TurnResult["toolCalls"];
  interrupted: boolean;
}

export class OpenAiCompatDriver implements Driver {
  readonly kind: string;
  private readonly spec: FolkProcessSpec;
  private readonly listeners = new Set<(e: DriverEvent) => void>();
  private readonly messages: ChatMessage[] = [];
  private tools: Array<{ type: "function"; function: { name: string; description: string; parameters: Record<string, unknown> } }> | null = null;
  private abort: AbortController | null = null;
  private pending: Pending | null = null;
  private readonly sessionId = randomUUID();

  constructor(spec: FolkProcessSpec) {
    this.spec = spec;
    this.kind = spec.engine.kind;
    this.messages.push({ role: "system", content: spec.systemPrompt });
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
    const endpoint = chatBase(s.engine.kind, s.engine.baseUrl);
    if (!endpoint) throw new Error(`${s.engine.kind}: no endpoint configured`);
    if (!s.engine.model) throw new Error(`${s.engine.kind}: no model configured`);
    const tools = await this.ensureTools();
    const pending: Pending = { text: [], toolCalls: [], interrupted: false };
    this.pending = pending;
    this.abort = new AbortController();
    this.emit({ type: "turn.started" });
    this.messages.push({ role: "user", content: text });
    let usage: Record<string, unknown> | null = null;
    try {
      for (let round = 0; round < MAX_ROUNDS; round++) {
        const reply = await this.complete(endpoint, s.engine.model, tools, this.abort.signal);
        if (reply.usage) usage = reply.usage;
        if (reply.content) pending.text.push(reply.content);
        if (reply.toolCalls.length === 0) {
          this.messages.push({ role: "assistant", content: reply.content ?? "" });
          if (reply.content) this.emit({ type: "text", text: reply.content });
          break;
        }
        this.messages.push({ role: "assistant", content: reply.content ?? null, tool_calls: reply.toolCalls });
        for (const call of reply.toolCalls) {
          const result = await this.runTool(call, pending);
          this.messages.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(result) });
        }
        if (pending.text.length) pending.text.push("\n\n");
      }
      const result: TurnResult = { text: pending.text.join("").trim(), costUsd: null, usage, interrupted: pending.interrupted, toolCalls: pending.toolCalls };
      this.emit({ type: "turn.completed", text: result.text, costUsd: null, usage, interrupted: result.interrupted });
      return result;
    } catch (err) {
      if (pending.interrupted) {
        const result: TurnResult = { text: pending.text.join("").trim(), costUsd: null, usage, interrupted: true, toolCalls: pending.toolCalls };
        this.emit({ type: "turn.completed", text: result.text, costUsd: null, usage, interrupted: true });
        return result;
      }
      const message = (err as Error).message;
      this.emit({ type: "error", message });
      throw err;
    } finally {
      this.pending = null;
      this.abort = null;
    }
  }

  async interrupt(): Promise<void> {
    if (!this.pending) return;
    this.pending.interrupted = true;
    this.abort?.abort();
  }

  async dispose(): Promise<void> {
    await this.interrupt();
    this.listeners.clear();
  }

  // ---- internals ------------------------------------------------------------------------

  private emit(e: DriverEvent): void {
    for (const cb of this.listeners) cb(e);
  }

  private async ensureTools() {
    if (this.tools) return this.tools;
    const defs = await this.spec.listEngineTools();
    const allowed = new Set(this.spec.engineTools);
    this.tools = defs
      .filter((d) => allowed.has(d.name))
      .map((d) => ({ type: "function" as const, function: { name: d.name, description: d.description, parameters: normaliseSchema(d.inputSchema) } }));
    this.emit({ type: "session", sessionId: this.sessionId, tools: this.tools.map((t) => t.function.name), version: `${this.spec.engine.kind} ${this.spec.engine.model ?? ""}`.trim() });
    return this.tools;
  }

  private async runTool(call: ToolCall, pending: Pending): Promise<unknown> {
    const name = call.function.name;
    let input: Record<string, unknown> = {};
    try {
      input = call.function.arguments ? (JSON.parse(call.function.arguments) as Record<string, unknown>) : {};
    } catch {
      input = { _raw: call.function.arguments };
    }
    const qualified = `mcp__engine__${name}`;
    this.emit({ type: "tool.started", id: call.id, tool: qualified, input });
    let output: unknown;
    let isError = false;
    if (!this.spec.engineTools.includes(name)) {
      output = { error: `${name} is not a tool this folk may call` };
      isError = true;
    } else {
      if (RISKY_ENGINE_TOOLS.has(name) && !input.approval_id) {
        const decision = await this.spec.approve(qualified, input);
        if (decision.behavior === "deny") {
          output = { error: "approval_required", message: decision.message ?? "The user did not approve this step." };
          isError = true;
        } else if (decision.updatedInput) {
          input = decision.updatedInput;
        }
      }
      if (!isError) {
        try {
          output = await this.spec.callEngine(name, input);
          isError = typeof output === "object" && output !== null && "error" in (output as Record<string, unknown>);
        } catch (err) {
          output = { error: (err as Error).message };
          isError = true;
        }
      }
    }
    this.emit({ type: "tool.done", id: call.id, tool: qualified, output, isError });
    pending.toolCalls.push({ tool: qualified, input, output, isError });
    return output;
  }

  /** One streamed chat completion. Resolves with the assembled text, tool calls and usage. */
  private async complete(endpoint: string, model: string, tools: NonNullable<typeof this.tools>, signal: AbortSignal) {
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.spec.engine.apiKey) headers.authorization = `Bearer ${this.spec.engine.apiKey}`;
    if (this.spec.engine.kind === "openrouter") {
      headers["HTTP-Referer"] = "https://github.com/openftth";
      headers["X-Title"] = "openFTTH";
    }
    const body: Record<string, unknown> = { model, messages: this.messages, stream: true, stream_options: { include_usage: true } };
    if (tools.length) {
      body.tools = tools;
      body.tool_choice = "auto";
    }
    const res = await fetch(`${endpoint}/chat/completions`, { method: "POST", headers, body: JSON.stringify(body), signal });
    if (!res.ok || !res.body) {
      const detail = await res.text().catch(() => "");
      throw new Error(`${this.spec.engine.kind} answered ${res.status}${detail ? `: ${detail.slice(0, 300)}` : ""}`);
    }
    let content = "";
    const calls = new Map<number, ToolCall>();
    let usage: Record<string, unknown> | null = null;
    const contentType = res.headers.get("content-type") ?? "";
    if (!contentType.includes("text/event-stream")) {
      // A server that ignores `stream`: one JSON body.
      const json = (await res.json()) as { choices?: Array<{ message?: { content?: string | null; tool_calls?: ToolCall[] } }>; usage?: Record<string, unknown> };
      const msg = json.choices?.[0]?.message;
      content = msg?.content ?? "";
      if (content) this.emit({ type: "text.delta", text: content });
      (msg?.tool_calls ?? []).forEach((c, i) => calls.set(i, c));
      usage = json.usage ?? null;
      return { content, toolCalls: [...calls.values()], usage };
    }
    for await (const data of sseData(res.body, signal)) {
      if (data === "[DONE]") break;
      let chunk: any;
      try {
        chunk = JSON.parse(data);
      } catch {
        continue;
      }
      if (chunk.usage) usage = chunk.usage;
      const delta = chunk.choices?.[0]?.delta;
      if (!delta) continue;
      if (typeof delta.content === "string" && delta.content) {
        content += delta.content;
        this.emit({ type: "text.delta", text: delta.content });
      }
      for (const tc of delta.tool_calls ?? []) {
        const index = typeof tc.index === "number" ? tc.index : calls.size;
        const existing = calls.get(index);
        if (!existing) {
          calls.set(index, { id: tc.id ?? `call_${index}_${randomUUID().slice(0, 8)}`, type: "function", function: { name: tc.function?.name ?? "", arguments: tc.function?.arguments ?? "" } });
        } else {
          if (tc.id && !existing.id) existing.id = tc.id;
          if (tc.function?.name) existing.function.name += tc.function.name;
          if (tc.function?.arguments) existing.function.arguments += tc.function.arguments;
        }
      }
    }
    return { content, toolCalls: [...calls.values()].filter((c) => c.function.name), usage };
  }
}

/** Yield the `data:` payloads of a server-sent event stream. */
async function* sseData(body: ReadableStream<Uint8Array>, signal: AbortSignal): AsyncGenerator<string> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).replace(/\r$/, "");
        buffer = buffer.slice(idx + 1);
        if (line.startsWith("data:")) yield line.slice(5).trim();
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Some servers reject schemas with `$schema`/`title`; keep the parts every model understands. */
function normaliseSchema(schema: Record<string, unknown>): Record<string, unknown> {
  const { $schema: _s, title: _t, ...rest } = schema as Record<string, unknown> & { $schema?: unknown; title?: unknown };
  if (!rest.type) rest.type = "object";
  if (rest.type === "object" && !rest.properties) rest.properties = {};
  return rest;
}

export const openAiDriverFactory = (spec: FolkProcessSpec): Driver => new OpenAiCompatDriver(spec);

export type { EngineToolDef };

/**
 * Where the OpenAI-style chat API lives. Ollama is configured by its own address (its native
 * API answers at `/api/...`); its OpenAI-compatible API is under `/v1`.
 */
export function chatBase(kind: string, baseUrl: string | null | undefined): string | undefined {
  const base = baseUrl?.replace(/\/+$/, "");
  if (!base) return undefined;
  return kind === "ollama" && !/\/v1$/.test(base) ? `${base}/v1` : base;
}
