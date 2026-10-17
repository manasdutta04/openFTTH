/**
 * Approvals: a risky tool call becomes a card; the engine's permission prompt waits on it.
 *
 * Risky engine tools are never on a folk's allow-list, so the engine CLI asks for permission.
 * The permission proxy posts here; the user answers through the app; on "allow" for a risky
 * engine tool the tool input gets an `approval_id` that the design engine verifies before acting.
 */

import { randomUUID } from "node:crypto";
import { join } from "node:path";
import type { EventBus } from "./events.ts";
import { readJsonOr, writeJsonAtomic } from "./store.ts";

export type ApprovalState = "pending" | "allowed" | "denied" | "expired";

export interface Approval {
  id: string;
  project: string;
  folk: string;
  tool: string; // fully qualified, e.g. mcp__engine__add_trench
  shortTool: string; // e.g. add_trench
  input: Record<string, unknown>;
  summary: string;
  state: ApprovalState;
  createdAt: number;
  decidedAt: number | null;
  note: string | null;
}

export interface Decision {
  behavior: "allow" | "deny";
  updatedInput?: Record<string, unknown>;
  message?: string;
}

export const RISKY_ENGINE_TOOLS = new Set(["add_trench", "route_outside_ducts", "export_for_construction", "rewind_design"]);

export class Approvals {
  private readonly pending = new Map<string, { approval: Approval; resolve: (d: Decision) => void; timer: NodeJS.Timeout }>();
  private readonly decided = new Map<string, Approval>();
  private readonly logPath: string;

  private readonly bus: EventBus;
  private readonly timeoutMs: number;

  constructor(bus: EventBus, dataDir: string, timeoutMs: number) {
    this.bus = bus;
    this.timeoutMs = timeoutMs;
    this.logPath = join(dataDir, "approvals.json");
    for (const a of readJsonOr<Approval[]>(this.logPath, [])) this.decided.set(a.id, a);
  }

  /** Open a card and wait for the user's decision (or the timeout, which denies). */
  request(input: { project: string; folk: string; tool: string; input: Record<string, unknown>; summary?: string }): Promise<Decision> {
    const shortTool = input.tool.replace(/^mcp__[^_]+(?:_[^_]+)*?__/, "");
    const approval: Approval = {
      id: randomUUID(),
      project: input.project,
      folk: input.folk,
      tool: input.tool,
      shortTool,
      input: input.input,
      summary: input.summary ?? describe(shortTool, input.input),
      state: "pending",
      createdAt: Date.now(),
      decidedAt: null,
      note: null,
    };
    return new Promise<Decision>((resolve) => {
      const timer = setTimeout(() => this.decide(approval.id, "deny", "timed out"), this.timeoutMs);
      timer.unref();
      this.pending.set(approval.id, { approval, resolve, timer });
      this.bus.emit("approval", publicView(approval));
    });
  }

  decide(id: string, decision: "allow" | "deny", note?: string): Approval | null {
    const entry = this.pending.get(id);
    if (!entry) return null;
    clearTimeout(entry.timer);
    this.pending.delete(id);
    const a = entry.approval;
    a.state = note === "timed out" ? "expired" : decision === "allow" ? "allowed" : "denied";
    a.decidedAt = Date.now();
    a.note = note ?? null;
    this.decided.set(a.id, a);
    this.persist();
    this.bus.emit("approval", publicView(a));
    if (decision === "allow") {
      const updatedInput = RISKY_ENGINE_TOOLS.has(a.shortTool) ? { ...a.input, approval_id: a.id } : undefined;
      entry.resolve(updatedInput ? { behavior: "allow", updatedInput } : { behavior: "allow" });
    } else {
      entry.resolve({ behavior: "deny", message: note ? `Denied by the user: ${note}` : "Denied by the user." });
    }
    return a;
  }

  get(id: string): Approval | null {
    return this.pending.get(id)?.approval ?? this.decided.get(id) ?? null;
  }

  list(): Approval[] {
    return [...[...this.pending.values()].map((p) => p.approval), ...this.decided.values()].sort((a, b) => b.createdAt - a.createdAt);
  }

  /** What the design engine asks: is this id an allowed approval for this tool? */
  check(id: string): { approved: boolean; tool: string | null; state: ApprovalState | null } {
    const a = this.get(id);
    if (!a) return { approved: false, tool: null, state: null };
    return { approved: a.state === "allowed", tool: a.shortTool, state: a.state };
  }

  private persist(): void {
    writeJsonAtomic(this.logPath, [...this.decided.values()].slice(-500));
  }
}

export function publicView(a: Approval): Record<string, unknown> {
  return { id: a.id, project: a.project, folk: a.folk, tool: a.shortTool, summary: a.summary, state: a.state, createdAt: a.createdAt, decidedAt: a.decidedAt, note: a.note, input: a.input };
}

function describe(tool: string, input: Record<string, unknown>): string {
  switch (tool) {
    case "add_trench":
      return `Dig a new trench (${String(input.surface ?? "verge")}) between two points`;
    case "route_outside_ducts":
      return "Route a cable outside the existing ducts";
    case "export_for_construction":
      return "Mark the current version as approved for construction and export it";
    case "rewind_design":
      return `Rewind the design to version ${String(input.version ?? "?").slice(0, 8)}`;
    default:
      return `Allow ${tool}`;
  }
}
