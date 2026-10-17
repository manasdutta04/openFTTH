/**
 * The project room: user turns, folk turns, hand-offs between folks, and the frames the app
 * draws from. One room per open project.
 */

import type { Crew, CrewMember, FolkMemory } from "./crew.ts";
import { mentions } from "./crew.ts";
import type { EventBus } from "./events.ts";
import type { Store } from "./store.ts";
import type { Driver, DriverEvent } from "./drivers/types.ts";

export interface DesignContext {
  selection?: string[];
  cursor?: [number, number];
  stage?: string;
  activeVersion?: string;
}

export type FolkState = "idle" | "thinking" | "tooling" | "waiting";

export interface RoomDeps {
  crew: Crew;
  bus: EventBus;
  store: Store;
  memory: FolkMemory;
  drivers: Map<string, Driver>;
  projectId: string;
  maxHops: number;
  log: (line: string) => void;
}

interface QueuedTurn {
  text: string;
  context: DesignContext | undefined;
  resolve: () => void;
}

export class Room {
  readonly id: string;
  private readonly states = new Map<string, FolkState>();
  private running = false;
  private current: { folk: CrewMember; driver: Driver } | null = null;
  private readonly queue: QueuedTurn[] = [];
  private readonly unsubscribe: Array<() => void> = [];
  private readonly d: RoomDeps;

  constructor(d: RoomDeps) {
    this.d = d;
    this.id = `room:${d.projectId}`;
    for (const m of d.crew.members) {
      this.states.set(m.key, "idle");
      const driver = d.drivers.get(m.key);
      if (driver) this.unsubscribe.push(driver.onEvent((e) => this.onDriverEvent(m, e)));
    }
  }

  folkStates(): Record<string, FolkState> {
    return Object.fromEntries(this.states);
  }

  get busy(): boolean {
    return this.running;
  }

  /** A user turn. Resolves when the whole chain (responder + hand-offs) has finished. */
  userTurn(text: string, context?: DesignContext): Promise<void> {
    const msg = this.d.store.addMessage({ room: this.id, role: "user", folk: null, kind: "text", text, json: context ? { context } : null });
    this.d.bus.emit("message", msg);
    return new Promise<void>((resolve) => {
      this.queue.push({ text, context, resolve });
      void this.pump();
    });
  }

  async interrupt(): Promise<void> {
    this.queue.splice(0, this.queue.length).forEach((q) => q.resolve());
    if (this.current) await this.current.driver.interrupt();
  }

  async dispose(): Promise<void> {
    await this.interrupt();
    this.unsubscribe.forEach((u) => u());
  }

  // ---- internals ------------------------------------------------------------------------

  private async pump(): Promise<void> {
    if (this.running) return;
    this.running = true;
    try {
      while (this.queue.length) {
        const turn = this.queue.shift()!;
        try {
          await this.runChain(turn.text, turn.context);
        } catch (err) {
          this.systemMessage(`turn failed: ${(err as Error).message}`, "error");
        } finally {
          turn.resolve();
        }
      }
    } finally {
      this.running = false;
    }
  }

  private async runChain(text: string, context: DesignContext | undefined): Promise<void> {
    const crew = this.d.crew;
    const mentioned = mentions(crew, text);
    let folk = mentioned[0] ?? crew.members.find((m) => m.key === crew.room.defaultResponder)!;
    let input = renderTurn(text, context, null);
    const visited: string[] = [];
    for (let hop = 0; hop <= this.d.maxHops; hop += 1) {
      visited.push(folk.key);
      const reply = await this.folkTurn(folk, input);
      if (reply === null) return; // interrupted
      const next = mentions(crew, reply).find((m) => m.key !== folk.key);
      if (!next) return;
      if (hop === this.d.maxHops) {
        this.systemMessage(`hand-off limit (${this.d.maxHops}) reached after ${visited.join(" → ")}`, "handoff");
        return;
      }
      this.d.store.addMessage({ room: this.id, role: "system", folk: null, kind: "handoff", text: `${folk.name} → ${next.name}`, json: { from: folk.key, to: next.key } });
      this.d.bus.emit("message", { room: this.id, role: "system", kind: "handoff", text: `${folk.name} → ${next.name}`, from: folk.key, to: next.key, at: Date.now() });
      input = renderTurn(`Hand-off from @${folk.name}:\n\n${reply}`, context, folk);
      folk = next;
    }
  }

  private async folkTurn(folk: CrewMember, input: string): Promise<string | null> {
    const driver = this.d.drivers.get(folk.key);
    if (!driver) {
      this.systemMessage(`no engine for ${folk.name}`, "error");
      return "";
    }
    this.current = { folk, driver };
    this.setState(folk.key, "thinking");
    const placeholder = this.d.store.addMessage({ room: this.id, role: "folk", folk: folk.key, kind: "text", text: "", json: { streaming: true } });
    this.d.bus.emit("message", placeholder);
    let streamed = "";
    const off = driver.onEvent((e) => {
      if (e.type === "text.delta") {
        streamed += e.text;
        this.d.bus.emit("message.patch", { id: placeholder.id, textDelta: e.text });
      }
    });
    try {
      const result = await driver.sendTurn(input);
      const text = result.text || streamed;
      this.d.store.updateMessageText(placeholder.id, text, { streaming: false, costUsd: result.costUsd, usage: result.usage, interrupted: result.interrupted, toolCalls: result.toolCalls.length });
      this.d.bus.emit("message", { ...placeholder, text, json: { streaming: false, costUsd: result.costUsd, interrupted: result.interrupted } });
      if (result.costUsd !== null) this.d.store.appendRuntime(this.d.projectId, { at: Date.now(), folk: folk.key, event: "turn.cost", detail: { costUsd: result.costUsd, usage: result.usage } });
      return result.interrupted ? null : text;
    } finally {
      off();
      this.current = null;
      this.setState(folk.key, "idle");
    }
  }

  private onDriverEvent(folk: CrewMember, e: DriverEvent): void {
    const bus = this.d.bus;
    switch (e.type) {
      case "tool.started": {
        this.setState(folk.key, "tooling");
        bus.emit("tool.activity", { folk: folk.key, tool: shortTool(e.tool), state: "started", id: e.id });
        this.runtime(folk.key, "tool.started", { tool: e.tool, input: e.input });
        break;
      }
      case "tool.done": {
        this.setState(folk.key, "thinking");
        const out = e.output as Record<string, unknown> | null;
        const summary = out && typeof out === "object" && typeof out.summary === "string" ? out.summary : undefined;
        bus.emit("tool.activity", { folk: folk.key, tool: shortTool(e.tool), state: e.isError ? "failed" : "done", id: e.id, ...(summary ? { summary } : {}) });
        this.runtime(folk.key, "tool.done", { tool: e.tool, isError: e.isError, summary: summary ?? null });
        this.d.store.addMessage({ room: this.id, role: "folk", folk: folk.key, kind: "tool", text: summary ?? `${shortTool(e.tool)} ${e.isError ? "failed" : "done"}`, json: { tool: shortTool(e.tool), isError: e.isError, output: trimOutput(out) } });
        if (out && typeof out === "object" && typeof out.version === "string" && out.version) {
          bus.emit("design.version", { version: out.version, tool: out.tool ?? shortTool(e.tool), summary: out.summary ?? "", changes: out.changes ?? null });
        }
        if (out && typeof out === "object" && out.error === "approval_required") {
          this.setState(folk.key, "waiting");
        }
        break;
      }
      case "error":
        this.runtime(folk.key, "error", { message: e.message });
        break;
      case "session":
        this.runtime(folk.key, "session", { sessionId: e.sessionId });
        break;
      case "exit":
        this.runtime(folk.key, "exit", { code: e.code });
        break;
      default:
        break;
    }
  }

  private setState(key: string, state: FolkState): void {
    if (this.states.get(key) === state) return;
    this.states.set(key, state);
    this.d.bus.emit("folk", { key, state });
  }

  private runtime(folk: string, event: string, detail: unknown): void {
    const ev = { at: Date.now(), folk, event, detail };
    this.d.store.appendRuntime(this.d.projectId, ev);
    this.d.bus.emit("runtime", ev);
  }

  private systemMessage(text: string, kind: "error" | "handoff"): void {
    const msg = this.d.store.addMessage({ room: this.id, role: "system", folk: null, kind, text, json: null });
    this.d.bus.emit("message", msg);
    this.d.log(text);
  }
}

/** The text a folk receives: design context as a bracketed prefix, never in the system prompt. */
export function renderTurn(text: string, context: DesignContext | undefined, from: CrewMember | null): string {
  const parts: string[] = [];
  if (context && (context.selection?.length || context.cursor || context.stage || context.activeVersion)) {
    const bits: string[] = [];
    if (context.stage) bits.push(`stage=${context.stage}`);
    if (context.activeVersion) bits.push(`version=${context.activeVersion.slice(0, 8)}`);
    if (context.selection?.length) bits.push(`selection=${context.selection.join(",")}`);
    if (context.cursor) bits.push(`cursor=${context.cursor[0].toFixed(6)},${context.cursor[1].toFixed(6)}`);
    parts.push(`[design context: ${bits.join("; ")}]`);
  }
  if (from) parts.push(`[from ${from.name}]`);
  parts.push(text);
  return parts.join("\n");
}

function shortTool(name: string): string {
  return name.replace(/^mcp__[a-z0-9_-]+?__/i, "");
}

function trimOutput(out: Record<string, unknown> | null): unknown {
  if (!out) return null;
  const s = JSON.stringify(out);
  return s.length > 8000 ? { truncated: true, head: s.slice(0, 8000) } : out;
}
