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
import { Room, toolTarget } from "./room.ts";
import { readJsonOr, writeJsonAtomic, type Store } from "./store.ts";
import type { Driver, DriverFactory } from "./drivers/types.ts";
import type { Approvals } from "./approvals.ts";
import type { Engines } from "./engines.ts";

export interface OpenProjectInput {
  /** The design area polygon (classic start), or omitted when `profile_path` is given. */
  area_path?: string;
  osm_path?: string;
  /** An import profile naming all the project's data sources (made by the import wizard). */
  profile_path?: string;
  rulebook?: string;
  id?: string;
}

export interface ProjectMeta {
  id: string;
  dir: string;
  areaPath: string;
  osmPath: string | null;
  /** The import profile the project's data comes from, when it was opened from one. */
  profilePath?: string | null;
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

/**
 * The engine tools behind the app's own controls: move a site, exclude a building, forbid
 * digging on a street. The app calls them through `steer()`; no crew member has them.
 */
export const STEER_TOOLS = ["move_site", "exclude_building", "forbid_street"] as const;
export type SteerTool = (typeof STEER_TOOLS)[number];

/** The engine's tool result, as every tool returns it. */
export interface SteerResult {
  tool: string;
  summary: string;
  version: string | null;
  changes: Record<string, number> | null;
  warnings: string[];
  decisions: unknown[];
  data: Record<string, unknown>;
}

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
    // A project reopened from Recent names its profile as its area: the profile is the area.
    const fromArea = input.area_path && /\.ya?ml$/i.test(input.area_path) ? input.area_path : undefined;
    const profileArg = input.profile_path ?? fromArea;
    const profilePath = profileArg ? resolve(profileArg) : null;
    if (profilePath && !existsSync(profilePath)) throw new HttpError(400, `import profile not found: ${profilePath}`);
    if (!profilePath && !input.area_path) throw new HttpError(400, "give an area_path or a profile_path");
    const areaPath = profilePath ?? resolve(input.area_path!);
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
    meta.profilePath = profilePath;
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

    this.room = new Room({ crew: this.d.crew, bus: this.d.bus, store: this.d.store, memory: this.memory, drivers: this.drivers, projectId: id, maxHops: this.d.cfg.maxHops, log: this.d.log, stageOf: async () => {
      const summary = await engine.call<{ stage?: unknown }>("design_summary");
      return typeof summary?.stage === "string" ? summary.stage : null;
    } });
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
        env: { OPENFTTH_HARNESS_URL: this.d.harnessUrl, OPENFTTH_COMMS_TOKEN: this.d.cfg.commsToken, OPENFTTH_FOLK: member.key, OPENFTTH_PROJECT: id, OPENFTTH_APPROVAL_TIMEOUT_MS: String(this.d.cfg.approvalTimeoutMs) },
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
        systemPrompt: systemPrompt(this.d.crew, member, this.memory.read(member.key), settings.kind, { areaPath: meta.areaPath, osmPath: meta.osmPath, profilePath: meta.profilePath ?? null, rulebook: meta.rulebook }),
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

  private automating = false;
  private autoRun: Promise<void> | null = null;

  /**
   * Design the open project from its inputs to deliverables with no AI: the engine's tools in
   * stage order. Returns at once; progress arrives as conversation lines and design frames.
   */
  autodesign(): { steps: number } {
    const meta = this.requireMeta();
    const room = this.room;
    const engine = this.requireEngine();
    if (!room) throw new HttpError(409, "no project open");
    if (!meta.osmPath && !meta.profilePath) {
      throw new HttpError(400, "choose a map extract (.osm) or import the project's data (File → Import data) first");
    }
    if (this.automating || room.busy) throw new HttpError(409, "the crew is working; wait for the turn to finish");
    this.automating = true;
    const started = Date.now();
    const total = AUTODESIGN_STEPS.length;
    this.autoRun = room
      .exclusive(async () => {
        room.note(`Automatic design started: ${total} engine steps, no AI involved.`, "text", { automatic: true });
        let last: Record<string, unknown> | null = null;
        for (const [i, step] of AUTODESIGN_STEPS.entries()) {
          let tool = step.tool;
          let args: Record<string, unknown> = {};
          if (step.tool === "import_osm") {
            if (meta.profilePath) {
              tool = "import_data";
              args = { profile_path: meta.profilePath };
            } else {
              args = { area_path: meta.areaPath, osm_path: meta.osmPath };
            }
          }
          this.d.bus.emit("tool.activity", { folk: null, tool: step.tool, state: "started", id: `auto-${i}` });
          let out: Record<string, unknown>;
          try {
            out = (await engine.call<Record<string, unknown>>(tool, args)) ?? {};
          } catch (err) {
            room.note(`${step.label} failed: ${(err as Error).message}`, "error");
            this.d.bus.emit("tool.activity", { folk: null, tool: step.tool, state: "failed", id: `auto-${i}` });
            return;
          }
          const summary = typeof out.summary === "string" ? out.summary : `${step.tool} done`;
          const failed = summary.startsWith("rejected") || typeof out.error === "string";
          this.d.bus.emit("tool.activity", { folk: null, tool: step.tool, state: failed ? "failed" : "done", id: `auto-${i}`, summary });
          room.note(summary, "tool", { tool: step.tool, isError: failed, step: i + 1, steps: total, automatic: true });
          if (typeof out.version === "string" && out.version) {
            this.d.bus.emit("design.version", { version: out.version, tool: step.tool, summary, changes: out.changes ?? null });
          }
          await room.refreshStage();
          if (failed) {
            room.note(`Automatic design stopped at "${step.label}". Ask the crew what to do, or fix the input and run it again.`, "error");
            return;
          }
          last = out;
        }
        const secs = Math.round((Date.now() - started) / 1000);
        room.note(`Automatic design finished in ${secs} s. Every number came from the engine's solvers and checks; open the BOQ tab or File → Export for the deliverables.${last && typeof last.summary === "string" ? ` Last step: ${last.summary}.` : ""}`, "text", { automatic: true });
      })
      .catch((err: Error) => room.note(`Automatic design could not start: ${err.message}`, "error"))
      .finally(() => {
        this.automating = false;
      });
    return { steps: total };
  }

  /** Resolves when the automatic design that is running (if any) has stopped. */
  async whenAutodesignDone(): Promise<void> {
    await this.autoRun;
  }

  /**
   * One engine tool for a caller outside the crew (the public MCP server). Tools that change the
   * design take the room for their run, so they never interleave with a crew turn.
   */
  async runTool<T = Record<string, unknown>>(tool: string, args: Record<string, unknown> = {}, opts: { exclusive?: boolean } = {}): Promise<T> {
    const engine = this.requireEngine();
    if (!opts.exclusive) return engine.call<T>(tool, args);
    const room = this.room;
    if (!room) throw new HttpError(409, "no project open");
    if (this.automating || room.busy) throw new HttpError(409, "the crew is working; wait for the turn to finish");
    return room.exclusive(async () => {
      const out = await engine.call<T>(tool, args);
      const o = out as Record<string, unknown> | null;
      if (o && typeof o.version === "string" && o.version) {
        this.d.bus.emit("design.version", { version: o.version, tool, summary: o.summary ?? null, changes: o.changes ?? null });
      }
      await room.refreshStage();
      return out;
    });
  }

  get isAutomating(): boolean {
    return this.automating;
  }

  /** A home's fibre from OLT port to drop, straight from the engine (no crew member). */
  async trace(uuid: string): Promise<unknown> {
    return this.requireEngine().call("trace_fibre", { element_uuid: uuid });
  }

  async assumptions(): Promise<unknown> {
    return this.requireEngine().call("list_assumptions");
  }

  /**
   * The engineer's own change, straight to the engine: no crew member in the loop. The room
   * is told what happened, the app gets a `design.version` frame, and, unless told not to,
   * the Checker is asked to validate the design again. An engine rejection is a 422 with
   * the engine's own words and leaves no trace in the room.
   */
  async steer(tool: string, args: Record<string, unknown>, revalidate: boolean): Promise<SteerResult> {
    if (!(STEER_TOOLS as readonly string[]).includes(tool)) throw new HttpError(400, `${tool} is not one of the steering tools (${STEER_TOOLS.join(", ")})`);
    const room = this.room;
    if (!room) throw new HttpError(409, "no project open");
    if (room.busy) throw new HttpError(409, "the crew is working; stop the turn or wait for it to finish");
    const activity = `steer-${Date.now()}`;
    this.d.bus.emit("tool.activity", { folk: "you", tool, state: "started", id: activity, ...toolTarget(args) });
    let out: SteerResult;
    try {
      out = await this.requireEngine().call<SteerResult>(tool, args);
    } finally {
      this.d.bus.emit("tool.activity", { folk: "you", tool, state: "done", id: activity });
    }
    if (!out || typeof out !== "object" || typeof out.summary !== "string") throw new HttpError(502, `the engine returned no result for ${tool}`);
    if (out.summary.startsWith("rejected")) throw new HttpError(422, out.summary.replace(/^rejected:\s*/, ""));
    const reason = typeof args.reason === "string" ? args.reason.trim() : "";
    const reran = Array.isArray(out.data?.reran) ? (out.data.reran as string[]) : [];
    room.steerMessage(`${out.summary}${reason ? ` — ${reason}` : ""}`, { tool, args, version: out.version, reran, warnings: out.warnings ?? [] });
    if (out.version) this.d.bus.emit("design.version", { version: out.version, tool, summary: out.summary, changes: out.changes ?? null });
    await room.refreshStage();
    if (revalidate) void room.userTurn(`@Checker ${checkerPrompt(tool, out, reason)}`);
    return out;
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

/**
 * The automatic design: every engine tool in stage order, with no LLM involved. The solvers
 * make every decision, so this is the whole design at no cost; the crew is for questions,
 * explanations and changes.
 */
export const AUTODESIGN_STEPS: Array<{ tool: string; label: string }> = [
  { tool: "import_osm", label: "Import the project's data" },
  { tool: "check_inputs", label: "Check the data" },
  { tool: "build_twin", label: "Build the street twin" },
  { tool: "set_pop", label: "Place the POP" },
  { tool: "cluster_homes", label: "Place FATs" },
  { tool: "place_fdh", label: "Place cabinets" },
  { tool: "assign_pon", label: "Group homes into PONs" },
  { tool: "route_feeder", label: "Route feeder cables" },
  { tool: "route_distribution", label: "Route distribution cables" },
  { tool: "route_drops", label: "Route drops" },
  { tool: "place_chambers", label: "Place chambers" },
  { tool: "size_cables", label: "Size cables" },
  { tool: "assign_fibers", label: "Assign fibres" },
  { tool: "check_rules", label: "Check the design rules" },
  { tool: "check_operator", label: "Check the operator rules" },
  { tool: "boq", label: "Bill of quantities" },
  { tool: "splice_schedule", label: "Splice schedule" },
  { tool: "export_gpkg", label: "GeoPackage" },
];

/** What the engineer asks the Checker after a change of their own. */
function checkerPrompt(tool: string, out: SteerResult, reason: string): string {
  const what = tool === "move_site" ? "moved a site by hand" : tool === "exclude_building" ? "excluded a building by hand" : "marked a street as no-dig";
  const reran = Array.isArray(out.data?.reran) && out.data.reran.length ? ` The engine re-ran ${(out.data.reran as string[]).join(", ")}.` : "";
  return `I ${what} (${out.summary}${reason ? `; reason: ${reason}` : ""}).${reran} Validate the design again and tell me what changed and whether anything now breaks a rule. Quote the recorded decisions.`;
}

export class HttpError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}
