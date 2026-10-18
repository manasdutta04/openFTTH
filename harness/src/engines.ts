/**
 * Folk engines: the LLM runtimes a folk can run on, which one is active, and whether each is
 * usable on this machine. Settings live in `<dataDir>/engines.json`; API keys never leave it.
 */

import { existsSync, statSync } from "node:fs";
import { join } from "node:path";
import type { FolkEngineConfig } from "./config.ts";
import { readJsonOr, writeJsonAtomic } from "./store.ts";

export type EngineKind = "claude" | "codex" | "hermes" | "ollama" | "openrouter" | "openai";

export const ENGINE_KINDS: EngineKind[] = ["claude", "codex", "hermes", "ollama", "openrouter", "openai"];

export interface EngineSettings {
  kind: EngineKind;
  model: string | null;
  baseUrl: string | null;
  apiKey: string | null;
}

export interface EngineInfo {
  kind: EngineKind;
  label: string;
  description: string;
  /** What the engine needs from the user before it can run. */
  needs: Array<"model" | "baseUrl" | "apiKey">;
  available: boolean;
  detail: string;
  models: string[];
  model: string | null;
  baseUrl: string | null;
  hasKey: boolean;
  current: boolean;
}

interface Catalog {
  label: string;
  description: string;
  needs: EngineInfo["needs"];
  defaultModel: string | null;
  defaultBaseUrl: string | null;
  keyEnv: string | null;
}

export const ENGINE_CATALOG: Record<EngineKind, Catalog> = {
  claude: {
    label: "Claude CLI",
    description: "Claude Code on this machine, with its own login. Tools and approvals run through its permission prompt.",
    needs: [],
    defaultModel: null,
    defaultBaseUrl: null,
    keyEnv: null,
  },
  codex: {
    label: "Codex CLI",
    description: "OpenAI Codex on this machine, with its own login. Runs read-only; engine tools over MCP.",
    needs: [],
    defaultModel: null,
    defaultBaseUrl: null,
    keyEnv: null,
  },
  hermes: {
    label: "Hermes Agent",
    description: "Hermes on this machine, speaking the Agent Client Protocol. It brings its own model and keys; the design engine is handed to it over MCP.",
    needs: [],
    defaultModel: null,
    defaultBaseUrl: null,
    keyEnv: null,
  },
  ollama: {
    label: "Ollama",
    description: "A local model served by Ollama. Pick a model that supports tool calling.",
    needs: ["model"],
    defaultModel: null,
    defaultBaseUrl: "http://127.0.0.1:11434",
    keyEnv: null,
  },
  openrouter: {
    label: "OpenRouter",
    description: "Hosted models through OpenRouter's API. Needs an API key.",
    needs: ["model", "apiKey"],
    defaultModel: "anthropic/claude-sonnet-4.5",
    defaultBaseUrl: "https://openrouter.ai/api/v1",
    keyEnv: "OPENROUTER_API_KEY",
  },
  openai: {
    label: "OpenAI-compatible endpoint",
    description: "Any server that speaks the OpenAI chat API with tools: LM Studio, vLLM, a hosted agent, or OpenAI itself.",
    needs: ["model", "baseUrl"],
    defaultModel: null,
    defaultBaseUrl: null,
    keyEnv: "OPENAI_API_KEY",
  },
};

const OPENROUTER_SUGGESTIONS = [
  "anthropic/claude-sonnet-4.5",
  "openai/gpt-5",
  "google/gemini-2.5-pro",
  "qwen/qwen3-coder",
  "deepseek/deepseek-chat-v3.1",
];

interface Stored {
  current: EngineKind;
  settings: Partial<Record<EngineKind, { model?: string | null; baseUrl?: string | null; apiKey?: string | null }>>;
}

export class Engines {
  private readonly path: string;
  private stored: Stored;

  constructor(dataDir: string, fallback: FolkEngineConfig) {
    this.path = join(dataDir, "engines.json");
    const kind = (ENGINE_KINDS as string[]).includes(fallback.kind) ? (fallback.kind as EngineKind) : "claude";
    this.stored = readJsonOr<Stored>(this.path, { current: kind, settings: fallback.model ? { [kind]: { model: fallback.model } } : {} });
    if (!(ENGINE_KINDS as string[]).includes(this.stored.current)) this.stored.current = kind;
    if (!this.stored.settings || typeof this.stored.settings !== "object") this.stored.settings = {};
  }

  /** The active engine with its secrets resolved (settings first, then the environment). */
  current(): EngineSettings {
    return this.resolved(this.stored.current);
  }

  resolved(kind: EngineKind): EngineSettings {
    const cat = ENGINE_CATALOG[kind];
    const s = this.stored.settings[kind] ?? {};
    return {
      kind,
      model: s.model ?? cat.defaultModel,
      baseUrl: s.baseUrl ?? (kind === "ollama" ? ollamaHost() ?? cat.defaultBaseUrl : cat.defaultBaseUrl),
      apiKey: s.apiKey ?? (cat.keyEnv ? process.env[cat.keyEnv] ?? null : null),
    };
  }

  /** Switch engines and/or update one engine's settings. Returns the new active engine. */
  set(input: { kind: string; model?: string | null; baseUrl?: string | null; apiKey?: string | null }): EngineSettings {
    if (!(ENGINE_KINDS as string[]).includes(input.kind)) throw new Error(`unknown engine kind "${input.kind}"`);
    const kind = input.kind as EngineKind;
    const prev = this.stored.settings[kind] ?? {};
    const next = { ...prev };
    if (input.model !== undefined) next.model = input.model?.trim() || null;
    if (input.baseUrl !== undefined) next.baseUrl = input.baseUrl?.trim().replace(/\/+$/, "") || null;
    if (input.apiKey !== undefined) next.apiKey = input.apiKey?.trim() || null;
    this.stored.settings[kind] = next;
    this.stored.current = kind;
    writeJsonAtomic(this.path, this.stored);
    return this.current();
  }

  /** The active engine as the API shows it: no secret, just whether one is set. */
  publicCurrent(): Omit<EngineSettings, "apiKey"> & { hasKey: boolean; label: string } {
    const c = this.current();
    return { kind: c.kind, model: c.model, baseUrl: c.baseUrl, hasKey: Boolean(c.apiKey), label: ENGINE_CATALOG[c.kind].label };
  }

  /** Every engine with a live availability probe (bounded to about two seconds each). */
  async list(): Promise<EngineInfo[]> {
    return Promise.all(ENGINE_KINDS.map((k) => this.describe(k)));
  }

  async describe(kind: EngineKind): Promise<EngineInfo> {
    const cat = ENGINE_CATALOG[kind];
    const r = this.resolved(kind);
    const probe = await this.probe(kind, r);
    return {
      kind,
      label: cat.label,
      description: cat.description,
      needs: cat.needs,
      available: probe.available,
      detail: probe.detail,
      models: probe.models,
      model: r.model,
      baseUrl: r.baseUrl,
      hasKey: Boolean(r.apiKey),
      current: kind === this.stored.current,
    };
  }

  private async probe(kind: EngineKind, r: EngineSettings): Promise<{ available: boolean; detail: string; models: string[] }> {
    switch (kind) {
      case "claude": {
        const exe = findOnPath("claude");
        return exe ? { available: true, detail: exe, models: [] } : { available: false, detail: "claude is not on PATH. Install Claude Code and sign in.", models: [] };
      }
      case "codex": {
        const exe = findOnPath("codex");
        return exe ? { available: true, detail: exe, models: [] } : { available: false, detail: "codex is not on PATH. Install the Codex CLI and sign in.", models: [] };
      }
      case "hermes": {
        const exe = findOnPath("hermes");
        return exe ? { available: true, detail: exe, models: [] } : { available: false, detail: "hermes is not on PATH. Install Hermes Agent and configure a provider.", models: [] };
      }
      case "ollama": {
        const base = r.baseUrl ?? "http://127.0.0.1:11434";
        try {
          const res = await fetchWithTimeout(`${base}/api/tags`, {}, 2000);
          if (!res.ok) return { available: false, detail: `${base} answered ${res.status}`, models: [] };
          const body = (await res.json()) as { models?: Array<{ name: string }> };
          const models = (body.models ?? []).map((m) => m.name).sort();
          return { available: models.length > 0, detail: models.length ? `running at ${base}, ${models.length} model${models.length === 1 ? "" : "s"}` : `running at ${base} but no models are pulled`, models };
        } catch {
          return { available: false, detail: `not running at ${base}. Start Ollama and pull a tool-capable model.`, models: [] };
        }
      }
      case "openrouter": {
        return r.apiKey ? { available: true, detail: "API key set", models: OPENROUTER_SUGGESTIONS } : { available: false, detail: "needs an API key (settings or OPENROUTER_API_KEY)", models: OPENROUTER_SUGGESTIONS };
      }
      case "openai": {
        if (!r.baseUrl) return { available: false, detail: "needs the server's base URL, e.g. http://127.0.0.1:1234/v1", models: [] };
        try {
          const res = await fetchWithTimeout(`${r.baseUrl}/models`, { headers: r.apiKey ? { authorization: `Bearer ${r.apiKey}` } : {} }, 2000);
          if (!res.ok) return { available: res.status === 401 ? false : true, detail: `${r.baseUrl} answered ${res.status}`, models: [] };
          const body = (await res.json()) as { data?: Array<{ id: string }> };
          const models = (body.data ?? []).map((m) => m.id).sort();
          return { available: true, detail: `reachable at ${r.baseUrl}`, models };
        } catch {
          return { available: false, detail: `${r.baseUrl} is not reachable`, models: [] };
        }
      }
    }
  }
}

async function fetchWithTimeout(url: string, init: RequestInit, ms: number): Promise<Response> {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), ms);
  try {
    return await fetch(url, { ...init, signal: ac.signal });
  } finally {
    clearTimeout(t);
  }
}

/** Resolve a bare command on PATH (with PATHEXT on Windows). Returns the file, or null. */
export function findOnPath(command: string): string | null {
  if (/[\\/]/.test(command)) return existsSync(command) ? command : null;
  const sep = process.platform === "win32" ? ";" : ":";
  const exts = process.platform === "win32" ? [".exe", ".cmd", ".bat", ""] : [""];
  for (const dir of (process.env.PATH ?? "").split(sep).filter(Boolean)) {
    for (const ext of exts) {
      const candidate = join(dir, command + ext);
      try {
        if (existsSync(candidate) && statSync(candidate).isFile()) return candidate;
      } catch {
        // unreadable PATH entry
      }
    }
  }
  return null;
}

/** OLLAMA_HOST as Ollama itself reads it: "host:port", maybe without a scheme, maybe 0.0.0.0. */
function ollamaHost(): string | null {
  const raw = process.env.OLLAMA_HOST?.trim();
  if (!raw) return null;
  const withScheme = /^https?:\/\//.test(raw) ? raw : `http://${raw}`;
  return withScheme.replace("0.0.0.0", "127.0.0.1").replace(/\/+$/, "");
}
