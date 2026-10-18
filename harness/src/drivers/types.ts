/**
 * Engine drivers: one LLM runtime per folk, behind a tiny interface. A driver may be a
 * long-lived CLI process (Claude, Codex) or an HTTP chat loop (Ollama, OpenRouter, any
 * OpenAI-compatible server); the room cannot tell the difference.
 */

import type { Decision } from "../approvals.ts";
import type { EngineSettings } from "../engines.ts";

export type DriverEvent =
  | { type: "session"; sessionId: string; tools?: string[]; version?: string }
  | { type: "turn.started" }
  | { type: "text.delta"; text: string }
  | { type: "text"; text: string }
  | { type: "tool.started"; id: string; tool: string; input: Record<string, unknown> }
  | { type: "tool.done"; id: string; tool: string; output: unknown; isError: boolean }
  | { type: "turn.completed"; text: string; costUsd: number | null; usage: Record<string, unknown> | null; interrupted: boolean }
  | { type: "error"; message: string }
  | { type: "exit"; code: number | null };

export interface TurnResult {
  text: string;
  costUsd: number | null;
  usage: Record<string, unknown> | null;
  interrupted: boolean;
  toolCalls: Array<{ tool: string; input: Record<string, unknown>; output: unknown; isError: boolean }>;
}

export interface Driver {
  readonly kind: string;
  /** Send one user turn and resolve when the folk finished (or was interrupted). */
  sendTurn(text: string): Promise<TurnResult>;
  /** Stop the running turn. */
  interrupt(): Promise<void>;
  onEvent(cb: (e: DriverEvent) => void): () => void;
  /** Kill the process and forget the session. */
  dispose(): Promise<void>;
  readonly busy: boolean;
}

/** One engine tool as the design engine advertises it over MCP. */
export interface EngineToolDef {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

export interface FolkProcessSpec {
  folkKey: string;
  folkName: string;
  projectId: string;
  systemPrompt: string;
  /** Fully qualified tool names allowed without a permission prompt (CLI drivers). */
  allowedTools: string[];
  disallowedTools: string[];
  /** Short engine tool names this folk may call (HTTP drivers expose exactly these). */
  engineTools: string[];
  mcpConfigPath: string;
  permissionTool: string;
  /** The active engine, with its model and (for HTTP engines) endpoint and key resolved. */
  engine: EngineSettings;
  model?: string;
  cwd: string;
  command: string;
  /** Arguments placed before the CLI flags (a wrapper script, for instance). */
  extraArgs: string[];
  /** The design engine's MCP endpoint and direct access to it for drivers that call tools themselves. */
  engineUrl: string;
  callEngine: (tool: string, args: Record<string, unknown>) => Promise<unknown>;
  listEngineTools: () => Promise<EngineToolDef[]>;
  /** Ask the user to approve a risky tool call (opens a card, waits for the decision). */
  approve: (tool: string, input: Record<string, unknown>) => Promise<Decision>;
  /** The stdio permission proxy, for CLI engines that need to ask for approval themselves. */
  mcpProxy: { command: string; args: string[]; env: Record<string, string> };
  log: (line: string) => void;
}

export type DriverFactory = (spec: FolkProcessSpec) => Driver;
