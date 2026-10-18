/**
 * Project service: one open project at a time. Opening a project starts its design-engine
 * MCP server, writes each folk's MCP config, creates the folk drivers and the room.
 */

import { createHash } from "node:crypto";
import { existsSync, readdirSync } from "node:fs";
import { basename, join, resolve } from "node:path";
import type { HarnessConfig } from "./config.ts";
import { FolkMemory, systemPrompt, type Crew } from "./crew.ts";
import { startEngine, type EngineHandle } from "./engine-process.ts";
import type { EventBus } from "./events.ts";
import { Room } from "./room.ts";
import { readJsonOr, writeJsonAtomic, type Store } from "./store.ts";
import type { Driver, DriverFactory } from "./drivers/types.ts";

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
  driverFactory: DriverFactory;
  harnessUrl: string;
  proxyPath: string;
  log: (line: string) => void;
  /** Tests: attach to an already-running engine instead of spawning one. */
  engineAttachUrl?: string;
}

export const DISALLOWED_FOR_FOLKS = ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "KillShell"];

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

    for (const member of this.d.crew.members) {
      const mcpConfigPath = join(dir, `mcp.${member.key}.json`);
      writeJsonAtomic(mcpConfigPath, {
        mcpServers: {
          engine: { type: "http", url: engine.url },
          openftth: {
            type: "stdio",
            command: process.execPath,
            args: [this.d.proxyPath],
            env: { OPENFTTH_HARNESS_URL: this.d.harnessUrl, OPENFTTH_COMMS_TOKEN: this.d.cfg.commsToken, OPENFTTH_FOLK: member.key, OPENFTTH_PROJECT: id },
          },
        },
      });
      const engineCfg = { ...this.d.cfg.folkEngine, ...(member.engine ?? {}) };
      const driver = this.d.driverFactory({
        folkKey: member.key,
        systemPrompt: systemPrompt(this.d.crew, member, this.memory.read(member.key)),
        allowedTools: member.tools.map((t) => `mcp__engine__${t}`),
        disallowedTools: DISALLOWED_FOR_FOLKS,
        mcpConfigPath,
        permissionTool: "mcp__openftth__approve",
        ...(engineCfg.model ? { model: engineCfg.model } : {}),
        cwd: dir,
        command: engineCfg.command,
        extraArgs: engineCfg.args ?? [],
        log: this.d.log,
      });
      this.drivers.set(member.key, driver);
    }

    this.room = new Room({ crew: this.d.crew, bus: this.d.bus, store: this.d.store, memory: this.memory, drivers: this.drivers, projectId: id, maxHops: this.d.cfg.maxHops, log: this.d.log });
    this.meta = meta;
    this.d.bus.emit("project", { id, engine: "ready", url: engine.url, rulebook });
    return meta;
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
