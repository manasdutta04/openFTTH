/**
 * Engine drivers: one long-lived LLM process per folk, behind a tiny interface.
 */

export type DriverEvent =
  | { type: "session"; sessionId: string }
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

export interface FolkProcessSpec {
  folkKey: string;
  systemPrompt: string;
  /** Fully qualified tool names allowed without a permission prompt. */
  allowedTools: string[];
  disallowedTools: string[];
  mcpConfigPath: string;
  permissionTool: string;
  model?: string;
  cwd: string;
  command: string;
  /** Arguments placed before the CLI flags (a wrapper script, for instance). */
  extraArgs: string[];
  log: (line: string) => void;
}

export type DriverFactory = (spec: FolkProcessSpec) => Driver;
