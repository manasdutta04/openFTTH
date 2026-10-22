/**
 * The local HTTP + SSE API. No framework: a small router over node:http with a Bearer token,
 * loopback-only Host/Origin checks and JSON bodies.
 */

import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { HarnessConfig } from "./config.ts";
import { RISKY_ENGINE_TOOLS, type Approvals } from "./approvals.ts";
import type { Engines } from "./engines.ts";
import { listRulebooks, type Crew } from "./crew.ts";
import { findRepoRoot } from "./config.ts";
import { join } from "node:path";
import { inspectData, listImportProfiles, saveImportProfile } from "./engine-cli.ts";
import { connectionInfo, handleMcpRequest, loadMcpSettings, saveMcpSettings, updateMcpSettings } from "./mcp-public.ts";
import type { EventBus } from "./events.ts";
import { DISALLOWED_FOR_FOLKS, HttpError, type ProjectService } from "./project.ts";
import type { Store } from "./store.ts";

export interface ServerDeps {
  cfg: HarnessConfig;
  bus: EventBus;
  store: Store;
  approvals: Approvals;
  engines: Engines;
  crew: Crew;
  projects: ProjectService;
  startedAt: number;
}

type Handler = (req: IncomingMessage, res: ServerResponse, params: Record<string, string>, url: URL) => Promise<void> | void;

interface Route {
  method: string;
  pattern: RegExp;
  keys: string[];
  handler: Handler;
  auth: "app" | "comms" | "none";
}

export function buildServer(d: ServerDeps): Server {
  const routes: Route[] = [];
  const add = (method: string, path: string, handler: Handler, auth: Route["auth"] = "app") => {
    const keys: string[] = [];
    const pattern = new RegExp("^" + path.replace(/:([a-zA-Z]+)/g, (_m, k: string) => {
      keys.push(k);
      return "([^/]+)";
    }) + "/?$");
    routes.push({ method, pattern, keys, handler, auth });
  };

  // ---- health & events ----------------------------------------------------------------
  add("GET", "/api/health", (_req, res) => json(res, 200, { ok: true, version: d.cfg.version, uptime_s: Math.round((Date.now() - d.startedAt) / 1000), project: d.projects.current?.id ?? null }), "none");
  add("GET", "/api/events", (req, res, _p, url) => {
    const since = req.headers["last-event-id"]?.toString() ?? url.searchParams.get("since") ?? undefined;
    d.bus.subscribe(res, since, { screens: url.searchParams.get("screens") !== "off" });
  });

  // ---- crew -------------------------------------------------------------------------------
  add("GET", "/api/engines", async (_req, res) => {
    json(res, 200, { current: d.engines.publicCurrent(), engines: await d.engines.list() });
  });
  add("POST", "/api/engine", async (req, res) => {
    const body = await readJson(req);
    if (typeof body.kind !== "string") throw new HttpError(400, "kind is required");
    let current;
    try {
      current = d.engines.set({ kind: body.kind, model: body.model, baseUrl: body.baseUrl, apiKey: body.apiKey });
    } catch (err) {
      throw new HttpError(400, (err as Error).message);
    }
    await d.projects.rebuildDrivers();
    const pub = d.engines.publicCurrent();
    d.bus.emit("engine", pub);
    void current;
    json(res, 200, pub);
  });

  add("GET", "/api/crew", (_req, res) => {
    const states = d.projects.currentRoom?.folkStates() ?? {};
    json(res, 200, {
      name: d.crew.name,
      description: d.crew.description,
      room: d.crew.room,
      members: d.crew.members.map((m) => ({ key: m.key, name: m.name, title: m.title, color: m.color ?? null, tools: m.tools, state: states[m.key] ?? "idle" })),
    });
  });

  // ---- project ----------------------------------------------------------------------------
  add("POST", "/api/project", async (req, res) => {
    const body = await readJson(req);
    if (typeof body.area_path !== "string" && typeof body.profile_path !== "string") throw new HttpError(400, "area_path or profile_path is required");
    const meta = await d.projects.open({ area_path: body.area_path, osm_path: body.osm_path, profile_path: body.profile_path, rulebook: body.rulebook, id: body.id });
    json(res, 201, meta);
  });
  add("GET", "/api/project", async (_req, res) => json(res, 200, await d.projects.status()));

  // ---- import: look inside data files and check a profile before any project exists ----------
  add("POST", "/api/import/inspect", async (req, res) => {
    const body = await readJson(req);
    const paths = Array.isArray(body.paths) ? body.paths.filter((p: unknown) => typeof p === "string") : [];
    if (!paths.length) throw new HttpError(400, "paths is required");
    try {
      json(res, 200, await inspectData(d.cfg.engine, paths));
    } catch (err) {
      throw new HttpError(422, (err as Error).message);
    }
  });
  add("POST", "/api/import/profile", async (req, res) => {
    const body = await readJson(req);
    if (!body.profile || typeof body.profile !== "object") throw new HttpError(400, "profile is required");
    const name = String(body.name ?? (body.profile as { name?: string }).name ?? "import");
    try {
      json(res, 200, await saveImportProfile(d.cfg, name, body.profile, typeof body.rulebook === "string" ? body.rulebook : undefined));
    } catch (err) {
      throw new HttpError(422, (err as Error).message);
    }
  });
  add("GET", "/api/import/profiles", (_req, res) => json(res, 200, listImportProfiles(d.cfg)));
  add("DELETE", "/api/project", async (req, res) => {
    const purge = new URL(req.url ?? "/", "http://127.0.0.1").searchParams.get("purge");
    if (purge === "1" || purge === "true") await d.projects.purge();
    else await d.projects.close();
    json(res, 200, { open: false });
  });
  add("GET", "/api/rulebooks", (_req, res) => json(res, 200, listRulebooks(join(findRepoRoot(), "rulebooks"))));
  add("GET", "/api/projects", (_req, res) => json(res, 200, d.projects.listProjects()));
  add("GET", "/api/project/assumptions", async (_req, res) => json(res, 200, await d.projects.assumptions()));
  add("GET", "/api/project/element/:uuid", async (_req, res, p) => json(res, 200, await d.projects.explainElement(p.uuid!)));
  add("GET", "/api/project/trace/:uuid", async (_req, res, p) => json(res, 200, await d.projects.trace(p.uuid!)));
  add("GET", "/api/project/versions", async (_req, res, _p, url) => json(res, 200, await d.projects.versions(Number(url.searchParams.get("limit") ?? 30))));
  add("GET", "/api/project/diff", async (_req, res, _p, url) => {
    const from = url.searchParams.get("from");
    const to = url.searchParams.get("to");
    if (!from || !to) throw new HttpError(400, "from and to are required");
    json(res, 200, await d.projects.diff(from, to));
  });
  add("POST", "/api/project/approve", async (req, res) => {
    const body = await readJson(req);
    if (typeof body.version !== "string") throw new HttpError(400, "version is required");
    json(res, 200, d.projects.approve(body.version));
  });
  add("POST", "/api/project/autodesign", (_req, res) => {
    json(res, 202, { accepted: true, ...d.projects.autodesign() });
  });
  add("POST", "/api/project/steer", async (req, res) => {
    const body = await readJson(req);
    if (typeof body.tool !== "string") throw new HttpError(400, "tool is required");
    const args = body.args && typeof body.args === "object" ? (body.args as Record<string, unknown>) : {};
    json(res, 200, await d.projects.steer(body.tool, args, body.revalidate !== false));
  });

  // ---- room -------------------------------------------------------------------------------
  add("POST", "/api/room/messages", async (req, res) => {
    const room = requireRoom();
    const body = await readJson(req);
    if (typeof body.text !== "string" || !body.text.trim()) throw new HttpError(400, "text is required");
    const done = room.userTurn(body.text, body.designContext);
    if (body.wait === true) {
      await done;
      json(res, 200, { accepted: true, completed: true, folks: room.folkStates() });
    } else {
      json(res, 202, { accepted: true, queued: room.busy, folks: room.folkStates() });
    }
  });
  add("GET", "/api/room/messages", (_req, res, _p, url) => {
    const room = requireRoom();
    json(res, 200, d.store.messages(room.id, Number(url.searchParams.get("since") ?? 0), Number(url.searchParams.get("limit") ?? 200)));
  });
  add("POST", "/api/room/interrupt", async (_req, res) => {
    await requireRoom().interrupt();
    json(res, 200, { interrupted: true });
  });

  // ---- approvals --------------------------------------------------------------------------
  add("GET", "/api/approvals", (_req, res) => json(res, 200, d.approvals.list().map((a) => ({ ...a }))));
  add("POST", "/api/approvals/:id", async (req, res, p) => {
    const body = await readJson(req);
    if (body.decision !== "allow" && body.decision !== "deny") throw new HttpError(400, "decision must be allow or deny");
    const a = d.approvals.decide(p.id!, body.decision, typeof body.note === "string" ? body.note : undefined);
    if (!a) throw new HttpError(404, "no pending approval with that id");
    json(res, 200, a);
  });

  // ---- backstage --------------------------------------------------------------------------
  add("GET", "/api/backstage", (_req, res, _p, url) => {
    const id = d.projects.current?.id;
    json(res, 200, id ? d.store.readRuntime(id, Number(url.searchParams.get("limit") ?? 200)) : []);
  });

  // ---- public MCP server for other AI apps (its own token, off until the engineer turns it on)
  const mcpDeps = { cfg: d.cfg, approvals: d.approvals, engines: d.engines, store: d.store, crew: d.crew, get projects() {
    return d.projects;
  } };
  for (const method of ["POST", "GET", "DELETE"]) {
    add(method, "/mcp", async (req, res) => handleMcpRequest(mcpDeps, req, res, method === "POST" ? await readJson(req) : undefined), "none");
  }
  add("GET", "/api/mcp", (req, res) => {
    json(res, 200, connectionInfo(d.cfg, loadMcpSettings(d.cfg.dataDir), mcpUrl(req)));
  });
  add("POST", "/api/mcp", async (req, res) => {
    const body = await readJson(req);
    const s = updateMcpSettings(d.cfg.dataDir, { ...(typeof body.enabled === "boolean" ? { enabled: body.enabled } : {}), rotate: body.rotate === true });
    const url = mcpUrl(req);
    saveMcpSettings(d.cfg.dataDir, { ...s, url });
    json(res, 200, connectionInfo(d.cfg, s, url));
  });

  // ---- internal (comms token): permission proxy and engine ------------------------------
  add("POST", "/api/internal/permission", async (req, res) => {
    const body = await readJson(req);
    const tool = String(body.tool ?? "?");
    const folk = String(body.folk ?? "?");
    if (DISALLOWED_FOR_FOLKS.includes(tool)) {
      // Defence in depth: the folk engine is started with these tools disabled, and even if a
      // prompt for one reaches us it is refused without opening a card.
      json(res, 200, { behavior: "deny", message: `${tool} is not available to the crew. Use the engine tools.` });
      return;
    }
    if (READ_ONLY_BUILTINS.has(tool)) {
      // Looking at files in the project folder never needs the engineer.
      json(res, 200, { behavior: "allow" });
      return;
    }
    const engineTool = tool.match(/^mcp__engine__(.+)$/)?.[1];
    if (engineTool && !RISKY_ENGINE_TOOLS.has(engineTool)) {
      // An ordinary engine tool that is not on this folk's list belongs to a teammate: say so
      // instead of waking the engineer with a card.
      const owner = d.crew.members.find((m) => m.tools.includes(engineTool));
      const hint = owner && owner.key !== folk ? ` It belongs to @${owner.name}; hand the step over.` : "";
      json(res, 200, { behavior: "deny", message: `${engineTool} is not one of your tools.${hint}` });
      return;
    }
    if (!engineTool) {
      json(res, 200, { behavior: "deny", message: `${tool} is not available to the crew.` });
      return;
    }
    const decision = await d.approvals.request({ project: String(body.project ?? d.projects.current?.id ?? "?"), folk, tool, input: (body.input ?? {}) as Record<string, unknown> });
    json(res, 200, decision);
  }, "comms");
  add("GET", "/api/internal/approvals/:id", (_req, res, p) => json(res, 200, d.approvals.check(p.id!)), "comms");

  const server = createServer(async (req, res) => {
    try {
      if (!isLoopback(req)) throw new HttpError(403, "loopback only");
      const url = new URL(req.url ?? "/", `http://${req.headers.host ?? "127.0.0.1"}`);
      const route = routes.find((r) => r.method === req.method && r.pattern.test(url.pathname));
      if (!route) throw new HttpError(404, `no route ${req.method} ${url.pathname}`);
      if (route.auth !== "none") {
        const token = bearer(req);
        const expected = route.auth === "app" ? d.cfg.appToken : d.cfg.commsToken;
        if (!token || token !== expected) throw new HttpError(401, "missing or invalid bearer token");
      }
      const m = url.pathname.match(route.pattern)!;
      const params: Record<string, string> = {};
      route.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1] ?? "")));
      await route.handler(req, res, params, url);
    } catch (err) {
      const status = err instanceof HttpError ? err.status : 500;
      if (!res.headersSent) json(res, status, { error: (err as Error).message });
      else res.end();
    }
  });
  return server;

  function requireRoom() {
    const room = d.projects.currentRoom;
    if (!room) throw new HttpError(409, "no project open");
    return room;
  }
}

function isLoopback(req: IncomingMessage): boolean {
  const host = (req.headers.host ?? "").replace(/:\d+$/, "");
  const okHost = host === "127.0.0.1" || host === "localhost" || host === "[::1]" || host === "";
  const origin = req.headers.origin;
  const okOrigin = !origin || /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(origin);
  return okHost && okOrigin;
}

function mcpUrl(req: IncomingMessage): string {
  return `http://${req.headers.host ?? "127.0.0.1"}/mcp`;
}

function bearer(req: IncomingMessage): string | null {
  const h = req.headers.authorization;
  if (!h || !h.startsWith("Bearer ")) return null;
  return h.slice(7).trim();
}

/** Built-in CLI tools that only read: allowed without a card. */
const READ_ONLY_BUILTINS = new Set(["Read", "Glob", "Grep", "ToolSearch", "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool"]);

export function json(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(text) });
  res.end(text);
}

async function readJson(req: IncomingMessage): Promise<Record<string, any>> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, any>;
  } catch {
    throw new HttpError(400, "invalid JSON body");
  }
}
