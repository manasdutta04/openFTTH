/**
 * Project service: one open project at a time. Opening a project starts its design-engine
 * MCP server, writes each folk's MCP config, creates the folk drivers and the room.
 */

import { createHash } from "node:crypto";
import { existsSync, readdirSync, rmSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import type { HarnessConfig } from "./config.ts";
import { FolkMemory, systemPrompt, type Crew } from "./crew.ts";
import { startEngine, type EngineHandle } from "./engine-process.ts";
import type { EventBus } from "./events.ts";
import { Room } from "./room.ts";
import { readJsonOr, writeJsonAtomic, type Store } from "./store.ts";
import type { Driver, DriverFactory } from "./drivers/types.ts";
import type { Approvals } from "./approvals.ts";
import type { Engines } from "./engines.ts";

export interface OpenProjectInput {
  area_path: string;
  osm_path?: string;
  rulebook?: string;
  id?: string;
}

export interface ProjectMeta {
  id: string;
  dir: string;
  areaPath: string;
  osmPath: string | null;
  rulebook: string;
  createdAt: number;
  approvedVersions: string[];
}

export interface ProjectDeps {
  cfg: HarnessConfig;
  crew: Crew;
  bus: EventBus;
  store: Store;
  approvals: Approvals;
  engines: Engines;
  driverFactory: DriverFactory;
  harnessUrl: string;
  proxyPath: string;
  log: (line: string) => void;
  /** Tests: attach to an already-running engine instead of spawning one. */
  engineAttachUrl?: string;
}

/**
 * Built-in CLI tools a folk never gets: anything that writes or runs code, reaches the network,
 * spawns agents, or talks to a terminal user that is not there. Read-only lookups (Read, Grep,
 * Glob) stay, as does ToolSearch, which the CLI needs to load the engine's MCP tools.
 */
export const DISALLOWED_FOR_FOLKS = [
  "Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "KillShell",
  "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree", "Skill", "Workflow",
  "SendMessage", "ListAgents", "RemoteTrigger", "ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "ReportFindings",
];

export class ProjectService {
  private meta: ProjectMeta | null = null;
  private engine: EngineHandle | null = null;
  private room: Room | null = null;
  private drivers = new Map<string, Driver>();
  readonly memory: FolkMemory;
  private readonly d: ProjectDeps;

  constructor(d: ProjectDeps) {
    this.d = d;
    this.memory = new FolkMemory(d.cfg.dataDir);
  }

  get current(): ProjectMeta | null {
    return this.meta;
  }

  get currentRoom(): Room | null {
    return this.room;
  }

  get engineHandle(): EngineHandle | null {
    return this.engine;
  }

  async open(input: OpenProjectInput): Promise<ProjectMeta> {
    const areaPath = resolve(input.area_path);
    if (!existsSync(areaPath)) throw new HttpError(400, `area file not found: ${areaPath}`);
    const osmPath = input.osm_path ? resolve(input.osm_path) : null;
    if (osmPath && !existsSync(osmPath)) throw new HttpError(400, `osm file not found: ${osmPath}`);
    const rulebook = input.rulebook ?? this.d.cfg.engine.rulebook;
    const id = input.id ?? projectId(areaPath, rulebook);
    await this.close();

    const dir = join(this.d.cfg.dataDir, "projects", id);
    const metaPath = join(dir, "harness.json");
    const existing = readJsonOr<ProjectMeta | null>(metaPath, null);
    const meta: ProjectMeta = existing ?? { id, dir, areaPath, osmPath, rulebook, createdAt: Date.now(), approvedVersions: [] };
    meta.areaPath = areaPath;
    meta.osmPath = osmPath;
    meta.rulebook = rulebook;
    writeJsonAtomic(metaPath, meta);

    this.d.log(`starting engine for project ${id}`);
    const engine = await startEngine(
      { ...this.d.cfg.engine, rulebook },
      {
        projectDir: dir,
        approvalsUrl: `${this.d.harnessUrl}/api/internal/approvals`,
        commsToken: this.d.cfg.commsToken,
        log: (l) => this.d.log(`[engine] ${l}`),
        ...(this.d.engineAttachUrl ? { attachUrl: this.d.engineAttachUrl } : {}),
      },
    );
    engine.onExit((code) => {
      this.d.log(`engine exited (${code})`);
      this.d.bus.emit("project", { id, engine: "exited", code });
    });
    this.engine = engine;

    this.createDrivers(meta, engine);

    this.room = new Room({ crew: this.d.crew, bus: this.d.bus, store: this.d.store, memory: this.memory, drivers: this.drivers, projectId: id, maxHops: this.d.cfg.maxHops, log: this.d.log });
    this.meta = meta;
    this.d.bus.emit("project", { id, engine: "ready", url: engine.url, rulebook });
    return meta;
  }

  /** Build one driver per folk on the active engine. */
  private createDrivers(meta: ProjectMeta, engine: EngineHandle): void {
    const { id, dir } = meta;
    const active = this.d.engines.current();
    for (const member of this.d.crew.members) {
      const mcpConfigPath = join(dir, `mcp.${member.key}.json`);
      const proxy = {
        command: process.execPath,
        args: [this.d.proxyPath],
        env: { OPENFTTH_HARNESS_URL: this.d.harnessUrl, OPENFTTH_COMMS_TOKEN: this.d.cfg.commsToken, OPENFTTH_FOLK: member.key, OPENFTTH_PROJECT: id },
      };
      writeJsonAtomic(mcpConfigPath, {
        mcpServers: {
          engine: { type: "http", url: engine.url },
          openftth: { type: "stdio", ...proxy },
        },
      });
      const engineCfg = { ...this.d.cfg.folkEngine, ...(member.engine ?? {}) };
      const settings = { ...active, ...(member.engine?.model ? { model: member.engine.model } : {}) };
      // CLI engines are named after their binary; a configured command only applies to its own kind.
      const command = engineCfg.kind === settings.kind && engineCfg.command ? engineCfg.command : settings.kind;
      const driver = this.d.driverFactory({
        folkKey: member.key,
        folkName: member.name,
        projectId: id,
        systemPrompt: systemPrompt(this.d.crew, member, this.memory.read(member.key), settings.kind, { areaPath: meta.areaPath, osmPath: meta.osmPath, rulebook: meta.rulebook }),
        allowedTools: member.tools.map((t) => `mcp__engine__${t}`),
        disallowedTools: DISALLOWED_FOR_FOLKS,
        engineTools: member.tools,
        mcpConfigPath,
        permissionTool: "mcp__openftth__approve",
        engine: settings,
        ...(settings.model ? { model: settings.model } : {}),
        cwd: dir,
        command,
        extraArgs: engineCfg.args ?? [],
        engineUrl: engine.url,
        callEngine: (tool, args) => engine.call(tool, args),
        listEngineTools: () => engine.listToolDefs(),
        approve: (tool, input) => this.d.approvals.request({ project: id, folk: member.key, tool, input }),
        mcpProxy: proxy,
        log: this.d.log,
      });
      this.drivers.set(member.key, driver);
    }
  }

  /** The engine changed: replace every folk's driver; the room keeps its queue and history. */
  async rebuildDrivers(): Promise<void> {
    if (!this.meta || !this.engine) return;
    if (this.room?.busy) throw new HttpError(409, "a turn is running; stop it before switching engines");
    const old = [...this.drivers.values()];
    this.drivers.clear();
    for (const d of old) await d.dispose();
    this.createDrivers(this.meta, this.engine);
    this.room?.rebind();
  }

  async status(): Promise<Record<string, unknown>> {
    if (!this.meta) return { open: false };
    let summary: unknown = null;
    try {
      summary = this.engine ? await this.engine.call("design_summary") : null;
    } catch (err) {
      summary = { error: (err as Error).message };
    }
    return { open: true, ...this.meta, engine: this.engine ? { url: this.engine.url, pid: this.engine.pid } : null, summary, folks: this.room?.folkStates() ?? {} };
  }

  async versions(limit = 30): Promise<unknown> {
    return this.requireEngine().call("versions", { limit });
  }

  async diff(from: string, to: string): Promise<unknown> {
    return this.requireEngine().call("diff_versions", { older: from, newer: to });
  }

  async explainElement(uuid: string): Promise<unknown> {
    return this.requireEngine().call("explain_element", { element_uuid: uuid });
  }

  async assumptions(): Promise<unknown> {
    return this.requireEngine().call("list_assumptions");
  }

  /** Every project folder under the data dir, newest first. */
  listProjects(): ProjectMeta[] {
    const root = join(this.d.cfg.dataDir, "projects");
    if (!existsSync(root)) return [];
    const out: ProjectMeta[] = [];
    for (const name of readdirSync(root)) {
      const meta = readJsonOr<ProjectMeta | null>(join(root, name, "harness.json"), null);
      if (meta) out.push(meta);
    }
    return out.sort((a, b) => b.createdAt - a.createdAt);
  }

  approve(version: string): ProjectMeta {
    const meta = this.requireMeta();
    if (!meta.approvedVersions.includes(version)) meta.approvedVersions.push(version);
    writeJsonAtomic(join(meta.dir, "harness.json"), meta);
    this.d.bus.emit("project", { id: meta.id, approved: version });
    return meta;
  }

  /** Close the project and delete everything it produced: design, versions, log, conversation. */
  async purge(): Promise<void> {
    const meta = this.requireMeta();
    if (this.room?.busy) throw new HttpError(409, "a turn is running; stop it before resetting");
    await this.close();
    rmSync(meta.dir, { recursive: true, force: true });
    this.d.store.deleteRoom(`room:${meta.id}`);
    this.d.store.clearRuntime(meta.id);
    this.d.approvals.forget(meta.id);
    this.d.bus.emit("project", { id: meta.id, purged: true });
  }

  async close(): Promise<void> {
    if (this.room) await this.room.dispose();
    for (const d of this.drivers.values()) await d.dispose();
    this.drivers = new Map();
    if (this.engine) await this.engine.stop();
    this.room = null;
    this.engine = null;
    this.meta = null;
  }

  private requireEngine(): EngineHandle {
    if (!this.engine) throw new HttpError(409, "no project open");
    return this.engine;
  }

  private requireMeta(): ProjectMeta {
    if (!this.meta) throw new HttpError(409, "no project open");
    return this.meta;
  }
}

export function projectId(areaPath: string, rulebook: string): string {
  const stem = basename(areaPath).replace(/\.[^.]+$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "project";
  const hash = createHash("sha1").update(`${areaPath}|${rulebook}`).digest("hex").slice(0, 6);
  return `${stem}-${hash}`;
}

export class HttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
