/**
 * The tools other AI apps get through openFTTH's MCP link (see mcp-link.ts): open a project,
 * import data, design automatically, ask the crew, check, trace, steer and read the
 * deliverables. Risky steps still open an approval card in the app; nothing outside the app
 * can approve them.
 */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { extname, join } from "node:path";
import { McpServer, ResourceTemplate } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import type { Approvals } from "./approvals.ts";
import type { HarnessConfig } from "./config.ts";
import { listRulebooks, type Crew } from "./crew.ts";
import { findRepoRoot } from "./config.ts";
import { inspectData, listImportProfiles, saveImportProfile } from "./engine-cli.ts";
import { ENGINE_CATALOG, type Engines } from "./engines.ts";
import { STEER_TOOLS, type ProjectService } from "./project.ts";
import type { Store } from "./store.ts";

export interface PublicMcpDeps {
  cfg: HarnessConfig;
  projects: ProjectService;
  approvals: Approvals;
  engines: Engines;
  store: Store;
  crew: Crew;
}

/** Serve one MCP request (streamable HTTP, stateless: a fresh server per request). */
export async function handleMcpRequest(deps: PublicMcpDeps, req: IncomingMessage, res: ServerResponse, body: unknown): Promise<void> {
  if (req.method !== "POST") {
    // Stateless: no server-initiated stream and no session to delete.
    const text = JSON.stringify({ jsonrpc: "2.0", error: { code: -32000, message: "Method not allowed." }, id: null });
    res.writeHead(405, { "content-type": "application/json", allow: "POST" }).end(text);
    return;
  }
  const server = buildPublicMcp(deps);
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  res.on("close", () => {
    void transport.close();
    void server.close();
  });
  await server.connect(transport);
  await transport.handleRequest(req, res, body);
}

type ToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };

const ok = (v: unknown): ToolResult => ({ content: [{ type: "text", text: typeof v === "string" ? v : JSON.stringify(v, null, 2) }] });
const fail = (message: string): ToolResult => ({ content: [{ type: "text", text: message }], isError: true });

/** Every tool answers with JSON text; any failure becomes an MCP tool error with its message. */
function guard<A>(fn: (args: A) => Promise<unknown> | unknown): (args: A) => Promise<ToolResult> {
  return async (args: A) => {
    try {
      return ok(await fn(args));
    } catch (err) {
      return fail((err as Error).message);
    }
  };
}

const TEXT_TYPES: Record<string, string> = { ".csv": "text/csv", ".json": "application/json", ".geojson": "application/geo+json", ".txt": "text/plain", ".md": "text/markdown", ".dxf": "image/vnd.dxf" };
const BLOB_TYPES: Record<string, string> = { ".png": "image/png", ".pdf": "application/pdf", ".gpkg": "application/geopackage+sqlite3", ".zip": "application/zip" };
const MAX_RESOURCE_BYTES = 20 * 1024 * 1024;

export function buildPublicMcp(d: PublicMcpDeps): McpServer {
  const s = new McpServer(
    { name: "openftth", title: "openFTTH", version: d.cfg.version },
    {
      instructions:
        "openFTTH designs FTTH/GPON fibre networks. Every number in a design comes from the engine's solvers and checks; never estimate a count, length or cost yourself, quote the tool results. Start with `status`. To design: `open_project` (an area polygon and an OpenStreetMap extract, or an import profile), then `design_automatically`, then `list_deliverables` and read them as resources. `ask_crew` talks to the four AI crew members inside the app. Risky steps (`export_for_construction`) open an approval card in the openFTTH app and wait for the engineer.",
    },
  );
  const room = () => {
    const r = d.projects.currentRoom;
    if (!r) throw new Error("no project open; call open_project first");
    return r;
  };
  const meta = () => {
    const m = d.projects.current;
    if (!m) throw new Error("no project open; call open_project first");
    return m;
  };
  /** Tell the engineer, in the project conversation, what an outside app is doing. */
  const tell = (text: string) => d.projects.currentRoom?.note(`${text} (requested by another AI app through MCP)`, "text", { external: true });

  // ---- reading -----------------------------------------------------------------------------
  s.registerTool(
    "status",
    { title: "Project status", description: "The open project (if any): its inputs, design stage, element counts, what the crew is doing, pending approvals, the crew's AI engine and what it costs.", annotations: { readOnlyHint: true } },
    guard(async () => {
      const st = await d.projects.status();
      const engine = d.engines.publicCurrent();
      const cat = ENGINE_CATALOG[engine.kind as keyof typeof ENGINE_CATALOG];
      return {
        ...st,
        automating: d.projects.isAutomating,
        pending_approvals: d.approvals.list().filter((a) => a.state === "pending").length,
        crew_engine: { ...engine, cost: cat?.cost ?? null, cost_note: cat?.costNote ?? null },
        deliverables: d.projects.current ? deliverables(d.projects.current.dir).map((f) => f.name) : [],
      };
    }),
  );
  s.registerTool("list_projects", { title: "Projects", description: "Projects opened before, newest first.", annotations: { readOnlyHint: true } }, guard(() => d.projects.listProjects()));
  s.registerTool("list_rulebooks", { title: "Rulebooks", description: "The design rulebook presets (splitter ratios, distances, costs per country or operator).", annotations: { readOnlyHint: true } }, guard(() => listRulebooks(join(findRepoRoot(), "rulebooks"))));
  s.registerTool("list_import_profiles", { title: "Import profiles", description: "Saved import profiles: which files and layers make up a project's data.", annotations: { readOnlyHint: true } }, guard(() => listImportProfiles(d.cfg)));
  s.registerTool("design_summary", { title: "Design summary", description: "Element counts, stage and head version of the open design.", annotations: { readOnlyHint: true } }, guard(() => d.projects.runTool("design_summary")));
  s.registerTool("list_assumptions", { title: "Assumptions", description: "Every assumption the engine recorded (for example homes estimated from building outlines).", annotations: { readOnlyHint: true } }, guard(() => d.projects.runTool("list_assumptions")));
  s.registerTool(
    "versions",
    { title: "Versions", description: "The design's version history: one version per engine step.", inputSchema: { limit: z.number().int().min(1).max(200).optional() }, annotations: { readOnlyHint: true } },
    guard(({ limit }: { limit?: number }) => d.projects.runTool("versions", { limit: limit ?? 30 })),
  );
  s.registerTool(
    "explain_element",
    { title: "Explain an element", description: "One element's attributes and every recorded decision that placed or sized it.", inputSchema: { element_uuid: z.string() }, annotations: { readOnlyHint: true } },
    guard(({ element_uuid }: { element_uuid: string }) => d.projects.runTool("explain_element", { element_uuid })),
  );
  s.registerTool(
    "trace_fibre",
    { title: "Trace a fibre", description: "A home's fibre path from the OLT port to its drop, hop by hop with the optical loss.", inputSchema: { element_uuid: z.string().describe("the home's element uuid") }, annotations: { readOnlyHint: true } },
    guard(({ element_uuid }: { element_uuid: string }) => d.projects.runTool("trace_fibre", { element_uuid })),
  );
  s.registerTool(
    "list_deliverables",
    { title: "Deliverables", description: "Files the design produced (BOQ, splice schedule, map, GeoPackage, validation). Read each through its resource URI.", annotations: { readOnlyHint: true } },
    guard(() => deliverables(meta().dir).map((f) => ({ ...f, uri: `openftth://deliverables/${encodeURIComponent(f.name)}` }))),
  );

  // ---- starting a project ------------------------------------------------------------------
  s.registerTool(
    "inspect_data",
    { title: "Inspect data files", description: "List the layers in GIS files (GeoPackage, Shapefile, GeoJSON, KML, FlatGeobuf, DXF, CSV) and the import profile the engine proposes for them: which layer is the area, streets, address register, existing ducts, poles, manholes or no-dig zones.", inputSchema: { paths: z.array(z.string()).min(1).describe("absolute file paths on this computer") }, annotations: { readOnlyHint: true } },
    guard(({ paths }: { paths: string[] }) => inspectData(d.cfg.engine, paths)),
  );
  s.registerTool(
    "save_import_profile",
    { title: "Save an import profile", description: "Save an import profile (usually the one inspect_data proposed, corrected) and read its data. Returns the profile path for open_project and the data-quality report.", inputSchema: { name: z.string().min(1), profile: z.record(z.unknown()), rulebook: z.string().optional() } },
    guard(({ name, profile, rulebook }: { name: string; profile: Record<string, unknown>; rulebook?: string }) => saveImportProfile(d.cfg, name, profile, rulebook)),
  );
  s.registerTool(
    "open_project",
    {
      title: "Open a project",
      description: "Create or open a project and start its design engine. Give either an area polygon (area_path) with an OpenStreetMap extract (osm_path), or an import profile (profile_path). Closes the project that was open.",
      inputSchema: { area_path: z.string().optional(), osm_path: z.string().optional(), profile_path: z.string().optional(), rulebook: z.string().optional().describe("a preset from list_rulebooks; default generic-gpon") },
    },
    guard(async (a: { area_path?: string; osm_path?: string; profile_path?: string; rulebook?: string }) => {
      if (d.projects.currentRoom?.busy) throw new Error("the crew is working in the open project; wait for it to finish");
      const m = await d.projects.open({ ...(a.area_path ? { area_path: a.area_path } : {}), ...(a.osm_path ? { osm_path: a.osm_path } : {}), ...(a.profile_path ? { profile_path: a.profile_path } : {}), ...(a.rulebook ? { rulebook: a.rulebook } : {}) });
      tell("Project opened");
      return m;
    }),
  );

  // ---- designing ---------------------------------------------------------------------------
  s.registerTool(
    "design_automatically",
    {
      title: "Design automatically",
      description: "Design the open project from its inputs to deliverables with the engine alone: import, twin, POP, FATs, cabinets, PONs, routes, chambers, cable sizes, fibres, rule checks, BOQ, splice schedule, GeoPackage. No AI and no cost; takes about a minute for a few hundred homes. With wait (default) it returns each step's result.",
      inputSchema: { wait: z.boolean().optional(), timeout_s: z.number().int().min(10).max(3600).optional() },
    },
    guard(async ({ wait, timeout_s }: { wait?: boolean; timeout_s?: number }) => {
      const r = room();
      const since = Date.now() - 1;
      if (d.projects.isAutomating || r.busy) throw new Error("the crew is working; wait for the turn to finish");
      tell("Automatic design requested");
      const started = d.projects.autodesign();
      if (wait === false) return { started: true, ...started, note: "progress arrives in the app; call status to see the stage" };
      const finished = await Promise.race([d.projects.whenAutodesignDone().then(() => true), sleep((timeout_s ?? 900) * 1000).then(() => false)]);
      const lines = d.store.messages(r.id, since, 500).filter((m) => m.role === "system").map((m) => `${m.kind === "error" ? "ERROR " : ""}${m.text}`);
      return { finished, steps: lines, deliverables: deliverables(meta().dir).map((f) => f.name) };
    }),
  );
  s.registerTool(
    "check_design",
    { title: "Check the design", description: "Run the design-rule and operator-rule checks on the open design: violations, warnings and the worst optical loss." },
    guard(async () => ({
      rules: await d.projects.runTool("check_rules", {}, { exclusive: true }),
      operator: await d.projects.runTool("check_operator", {}, { exclusive: true }),
    })),
  );
  s.registerTool(
    "steer",
    {
      title: "Change the design",
      description: "The engineer's kind of change, made directly on the engine: move_site (element_uuid, lon, lat), exclude_building (element_uuid) or forbid_street (osm_id, no new trench). A reason is required and recorded. The engine re-runs what depends on the change.",
      inputSchema: {
        tool: z.enum(STEER_TOOLS),
        reason: z.string().min(3),
        element_uuid: z.string().optional(),
        osm_id: z.string().optional(),
        lon: z.number().optional(),
        lat: z.number().optional(),
      },
    },
    guard(async (a: { tool: (typeof STEER_TOOLS)[number]; reason: string; element_uuid?: string; osm_id?: string; lon?: number; lat?: number }) => {
      const { tool, ...rest } = a;
      const args = Object.fromEntries(Object.entries(rest).filter(([, v]) => v !== undefined));
      return d.projects.steer(tool, args, false);
    }),
  );
  s.registerTool(
    "ask_crew",
    {
      title: "Ask the crew",
      description: "Send a message to the crew inside the app (Surveyor, Planner, Checker, Drafter; address one with @Name). They work with the engine tools and answer in the project conversation. This runs the AI engine chosen in the app, which may cost money: see status.crew_engine. With wait (default) it returns their replies.",
      inputSchema: { text: z.string().min(1), wait: z.boolean().optional(), timeout_s: z.number().int().min(10).max(3600).optional() },
    },
    guard(async ({ text, wait, timeout_s }: { text: string; wait?: boolean; timeout_s?: number }) => {
      const r = room();
      if (d.projects.isAutomating) throw new Error("the automatic design is running; wait for it to finish");
      const since = Date.now() - 1;
      const done = r.userTurn(text);
      if (wait === false) return { accepted: true, queued: r.busy };
      const finished = await Promise.race([done.then(() => true), sleep((timeout_s ?? 900) * 1000).then(() => false)]);
      const replies = d.store
        .messages(r.id, since, 500)
        .filter((m) => m.role !== "user" && m.kind !== "tool")
        .map((m) => ({ from: m.folk ? (d.crew.members.find((c) => c.key === m.folk)?.name ?? m.folk) : "openFTTH", kind: m.kind, text: m.text }));
      return { finished, replies };
    }),
  );
  s.registerTool(
    "export_for_construction",
    {
      title: "Export for construction",
      description: "RISKY: mark the current design version as approved for construction and export it. This opens an approval card in the openFTTH app and waits for the engineer to allow or deny it there; it cannot be approved from here.",
      inputSchema: { why: z.string().optional().describe("shown to the engineer on the approval card") },
      annotations: { destructiveHint: true },
    },
    guard(async ({ why }: { why?: string }) => {
      const m = meta();
      tell("Export for construction requested; waiting for your approval");
      const decision = await d.approvals.request({
        project: m.id,
        folk: "external",
        tool: "export_for_construction",
        input: {},
        summary: `Another AI app asks to mark the current version approved for construction and export it${why ? `: ${why}` : ""}`,
      });
      if (decision.behavior !== "allow") throw new Error(decision.message ?? "The engineer denied the export.");
      return d.projects.runTool("export_for_construction", decision.updatedInput ?? {}, { exclusive: true });
    }),
  );

  // ---- resources ---------------------------------------------------------------------------
  s.registerResource("status", "openftth://project/status", { title: "Project status", mimeType: "application/json" }, async (uri) => ({
    contents: [{ uri: uri.href, mimeType: "application/json", text: JSON.stringify(await d.projects.status(), null, 2) }],
  }));
  const fileResource = (folder: "deliverables" | "design", title: string) =>
    s.registerResource(
      folder,
      new ResourceTemplate(`openftth://${folder}/{name}`, {
        list: async () => {
          const m = d.projects.current;
          const files = m ? listFiles(join(m.dir, folder)) : [];
          return { resources: files.map((f) => ({ uri: `openftth://${folder}/${encodeURIComponent(f.name)}`, name: f.name, mimeType: mimeOf(f.name), size: f.bytes })) };
        },
      }),
      { title, description: `Files in the open project's ${folder} folder.` },
      async (uri, vars) => {
        const name = decodeURIComponent(String(vars.name ?? ""));
        if (!name || /[\\/]|^\.\.?$/.test(name)) throw new Error("not a file name");
        const path = join(meta().dir, folder, name);
        if (!existsSync(path)) throw new Error(`${folder}/${name} does not exist`);
        const bytes = statSync(path).size;
        if (bytes > MAX_RESOURCE_BYTES) throw new Error(`${name} is ${Math.round(bytes / 1e6)} MB; open it from ${path}`);
        const mimeType = mimeOf(name);
        const ext = extname(name).toLowerCase();
        if (BLOB_TYPES[ext]) return { contents: [{ uri: uri.href, mimeType, blob: readFileSync(path).toString("base64") }] };
        return { contents: [{ uri: uri.href, mimeType, text: readFileSync(path, "utf8") }] };
      },
    );
  fileResource("deliverables", "Deliverables");
  fileResource("design", "Design layers (GeoJSON)");

  // ---- prompts -----------------------------------------------------------------------------
  s.registerPrompt(
    "design_area",
    { title: "Design this area", description: "Design an FTTH network for an area from its polygon and map extract.", argsSchema: { area_path: z.string(), osm_path: z.string().optional(), rulebook: z.string().optional() } },
    ({ area_path, osm_path, rulebook }) => userPrompt(
      `Design an FTTH network with openFTTH for the area in ${area_path}${osm_path ? ` using the OpenStreetMap extract ${osm_path}` : ""}${rulebook ? ` and the rulebook ${rulebook}` : ""}. ` +
        "Call open_project, then design_automatically. Then read openftth://deliverables/summary.json, boq.json and validation.json and report: homes and units served, FATs, cabinets and PONs, cable lengths, the BOQ total and cost per unit, and every rule warning. Quote only numbers from the tools; do not estimate.",
    ),
  );
  s.registerPrompt(
    "review_design",
    { title: "Review the design", description: "Check the open design and explain what an engineer should look at." },
    () => userPrompt("Review the open openFTTH design: call status, check_design and list_assumptions, read openftth://deliverables/validation.json, and list what an engineer must look at before construction, most important first. Quote the recorded reasons; do not estimate numbers."),
  );
  s.registerPrompt(
    "explain_costs",
    { title: "Explain the costs", description: "Break down the bill of quantities of the open design." },
    () => userPrompt("Read openftth://deliverables/boq.json from openFTTH and explain the cost: the largest lines, cost per unit served, and which design choices drive it (trench surfaces, cable lengths, cabinets). Quote only the numbers in the file."),
  );
  return s;
}

function userPrompt(text: string) {
  return { messages: [{ role: "user" as const, content: { type: "text" as const, text } }] };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms).unref());
}

function mimeOf(name: string): string {
  const ext = extname(name).toLowerCase();
  return TEXT_TYPES[ext] ?? BLOB_TYPES[ext] ?? "application/octet-stream";
}

function listFiles(dir: string): Array<{ name: string; bytes: number }> {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { withFileTypes: true })
    .filter((e) => e.isFile())
    .map((e) => ({ name: e.name, bytes: statSync(join(dir, e.name)).size }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

function deliverables(projectDir: string): Array<{ name: string; bytes: number }> {
  return listFiles(join(projectDir, "deliverables"));
}
