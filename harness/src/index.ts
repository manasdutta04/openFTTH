/**
 * Entry point: `node src/index.ts` (dev) or `node dist/index.js` (bundled).
 * `node … --proxy permission` runs the permission proxy from the same bundle.
 */

import { existsSync } from "node:fs";
import { join } from "node:path";
import { Approvals } from "./approvals.ts";
import { loadConfig, type HarnessConfig } from "./config.ts";
import { loadCrew } from "./crew.ts";
import { selectDriver } from "./drivers/index.ts";
import { Engines } from "./engines.ts";
import { EventBus } from "./events.ts";
import { ProjectService } from "./project.ts";
import { buildServer } from "./server.ts";
import { Store } from "./store.ts";
import type { DriverFactory } from "./drivers/types.ts";

export interface Harness {
  cfg: HarnessConfig;
  url: string;
  bus: EventBus;
  store: Store;
  approvals: Approvals;
  engines: Engines;
  projects: ProjectService;
  close(): Promise<void>;
}

export interface StartOptions {
  cfg?: Partial<HarnessConfig>;
  driverFactory?: DriverFactory;
  engineAttachUrl?: string;
  log?: (line: string) => void;
}

export async function startHarness(opts: StartOptions = {}): Promise<Harness> {
  const cfg = loadConfig(opts.cfg ?? {});
  const log = opts.log ?? ((l: string) => process.stderr.write(`[harness] ${l}\n`));
  const bus = new EventBus();
  const store = new Store(cfg.dataDir);
  const approvals = new Approvals(bus, cfg.dataDir, cfg.approvalTimeoutMs);
  const crew = loadCrew(cfg.crewPath);
  const engines = new Engines(cfg.dataDir, cfg.folkEngine);
  const startedAt = Date.now();

  // Listen first so the real port is known before the project service needs the URL.
  const projectsRef: { current: ProjectService | null } = { current: null };
  const server = buildServer({
    cfg,
    bus,
    store,
    approvals,
    engines,
    crew,
    get projects() {
      return projectsRef.current!;
    },
    startedAt,
  });
  // A permission prompt holds its HTTP request open until the engineer answers the card, which
  // can take longer than Node's default 5-minute request timeout; without this the proxy saw
  // "fetch failed" instead of a decision.
  server.requestTimeout = 0;
  server.headersTimeout = 0;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(cfg.port, cfg.host, () => resolve());
  });
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : cfg.port;
  const url = `http://127.0.0.1:${port}`;

  projectsRef.current = new ProjectService({
    cfg: { ...cfg, port },
    crew,
    bus,
    store,
    approvals,
    engines,
    driverFactory: opts.driverFactory ?? selectDriver,
    harnessUrl: url,
    proxyPath: proxyScriptPath(),
    log,
    ...(opts.engineAttachUrl ? { engineAttachUrl: opts.engineAttachUrl } : {}),
  });

  log(`listening on ${url} (data ${cfg.dataDir}, crew ${crew.name})`);
  return {
    cfg: { ...cfg, port },
    url,
    bus,
    store,
    approvals,
    engines,
    projects: projectsRef.current,
    async close() {
      await projectsRef.current?.close();
      bus.close();
      await new Promise<void>((resolve) => server.close(() => resolve()));
      store.close();
    },
  };
}

/** The permission proxy next to this file: `.ts` in a checkout, `.js` in the bundle. */
export function proxyScriptPath(): string {
  const dir = import.meta.dirname;
  for (const name of ["permission-proxy.ts", "permission-proxy.js"]) {
    const p = join(dir, name);
    if (existsSync(p)) return p;
  }
  return join(dir, "permission-proxy.js");
}

const isMain = process.argv[1] && import.meta.filename && process.argv[1].replace(/\\/g, "/").endsWith(import.meta.filename.replace(/\\/g, "/").split("/").pop()!);
if (isMain) {
  if (process.argv.includes("--proxy")) {
    await import("./permission-proxy.ts");
  } else {
    const h = await startHarness();
    const stop = async () => {
      await h.close();
      process.exit(0);
    };
    process.on("SIGINT", () => void stop());
    process.on("SIGTERM", () => void stop());
  }
}
