/**
 * Harness configuration: environment first, then `<dataDir>/config.json`, then defaults.
 *
 * The app that spawns the harness passes OPENFTTH_APP_TOKEN (required on every /api call except
 * /api/health) and OPENFTTH_DATA_DIR. OPENFTTH_COMMS_TOKEN is generated per boot and handed to
 * child processes (engine, permission proxy) for the /api/internal routes.
 */

import { randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export interface EngineConfig {
  /** Command that runs the design engine CLI, e.g. ["uv", "run", "openftth-engine"] */
  command: string[];
  /** Working directory for the engine command (the engine/ folder of a checkout, or the bundled runtime dir) */
  cwd: string;
  /** Rulebook preset or path */
  rulebook: string;
}

export interface FolkEngineConfig {
  /** "claude" | "codex" | "fake" */
  kind: string;
  /** Executable, e.g. "claude" */
  command: string;
  args?: string[];
  model?: string;
}

export interface HarnessConfig {
  dataDir: string;
  port: number;
  host: string;
  appToken: string;
  commsToken: string;
  crewPath: string;
  engine: EngineConfig;
  /** Default engine for every folk; the crew manifest can override per member. */
  folkEngine: FolkEngineConfig;
  version: string;
  /** Max folk-to-folk hand-offs per user turn. */
  maxHops: number;
  /** Approval timeout; a pending approval is denied after this. */
  approvalTimeoutMs: number;
}

const VERSION = "0.1.0";

export function loadConfig(overrides: Partial<HarnessConfig> = {}): HarnessConfig {
  const env = process.env;
  const dataDir = resolve(overrides.dataDir ?? env.OPENFTTH_DATA_DIR ?? join(homedir(), ".openftth"));
  mkdirSync(dataDir, { recursive: true });
  const fileCfg = readJson(join(dataDir, "config.json"));
  const repoRoot = findRepoRoot();

  const engineCwd = resolve(fileCfg.engine?.cwd ?? env.OPENFTTH_ENGINE_CWD ?? join(repoRoot, "engine"));
  const engine: EngineConfig = {
    command: fileCfg.engine?.command ?? splitCommand(env.OPENFTTH_ENGINE_COMMAND) ?? defaultEngineCommand(engineCwd),
    cwd: engineCwd,
    rulebook: fileCfg.engine?.rulebook ?? env.OPENFTTH_RULEBOOK ?? "generic-gpon",
  };
  const folkEngine: FolkEngineConfig = {
    kind: fileCfg.folkEngine?.kind ?? env.OPENFTTH_FOLK_ENGINE ?? "claude",
    command: fileCfg.folkEngine?.command ?? env.OPENFTTH_FOLK_ENGINE_COMMAND ?? "claude",
    ...(fileCfg.folkEngine?.args ? { args: fileCfg.folkEngine.args as string[] } : {}),
    ...(fileCfg.folkEngine?.model ?? env.OPENFTTH_FOLK_MODEL ? { model: fileCfg.folkEngine?.model ?? env.OPENFTTH_FOLK_MODEL } : {}),
  };

  const appToken = overrides.appToken ?? env.OPENFTTH_APP_TOKEN ?? fileCfg.appToken;
  if (!appToken) {
    throw new Error("OPENFTTH_APP_TOKEN is required (the app generates it per launch)");
  }

  return {
    dataDir,
    port: overrides.port ?? Number(env.OPENFTTH_PORT ?? fileCfg.port ?? 8799),
    host: "127.0.0.1",
    appToken,
    commsToken: overrides.commsToken ?? env.OPENFTTH_COMMS_TOKEN ?? randomBytes(24).toString("hex"),
    crewPath: resolve(overrides.crewPath ?? env.OPENFTTH_CREW ?? fileCfg.crewPath ?? join(repoRoot, "crews", "openftth", "crew.openftth.json")),
    engine: overrides.engine ?? engine,
    folkEngine: overrides.folkEngine ?? folkEngine,
    version: VERSION,
    maxHops: overrides.maxHops ?? Number(fileCfg.maxHops ?? 10),
    approvalTimeoutMs: overrides.approvalTimeoutMs ?? Number(fileCfg.approvalTimeoutMs ?? 15 * 60 * 1000),
  };
}

function readJson(path: string): Record<string, any> {
  if (!existsSync(path)) return {};
  try {
    return JSON.parse(readFileSync(path, "utf8")) as Record<string, any>;
  } catch (err) {
    throw new Error(`invalid JSON in ${path}: ${(err as Error).message}`);
  }
}

function splitCommand(value: string | undefined): string[] | undefined {
  if (!value) return undefined;
  return value.split(/\s+/).filter(Boolean);
}

/** The repo root when running from a checkout (harness/ sits next to engine/ and crews/). */
export function findRepoRoot(): string {
  let dir = resolve(import.meta.dirname ?? process.cwd());
  for (let i = 0; i < 5; i += 1) {
    if (existsSync(join(dir, "crews")) && existsSync(join(dir, "engine"))) return dir;
    dir = resolve(dir, "..");
  }
  return process.cwd();
}

/**
 * How to start the design engine when nothing is configured. The engine's own environment
 * (`engine/.venv`, made by `uv sync`) has a ready executable: using it needs no `uv` on the
 * PATH, which an app started from the Start menu or a desktop shortcut usually lacks.
 */
function defaultEngineCommand(engineCwd: string): string[] {
  // The installed app ships its own Python with the engine in it (engine/python).
  const bundled = join(engineCwd, "python", process.platform === "win32" ? "python.exe" : "bin/python3");
  if (existsSync(bundled)) return [bundled, "-m", "openftth_engine"];
  const exe = process.platform === "win32"
    ? join(engineCwd, ".venv", "Scripts", "openftth-engine.exe")
    : join(engineCwd, ".venv", "bin", "openftth-engine");
  if (existsSync(exe)) return [exe];
  const uvHome = join(homedir(), ".local", "bin", process.platform === "win32" ? "uv.exe" : "uv");
  return [existsSync(uvHome) ? uvHome : "uv", "run", "openftth-engine"];
}
