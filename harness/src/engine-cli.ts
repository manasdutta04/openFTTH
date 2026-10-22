/**
 * One-shot calls to the design engine's command line, for work that happens before a project
 * (and its engine process) exists: looking inside data files, checking an import profile.
 */

import { spawn } from "node:child_process";
import type { EngineConfig } from "./config.ts";

/** Run `openftth-engine <args>` and parse the JSON it prints on its last stdout line. */
export function runEngineJson<T>(cfg: EngineConfig, args: string[], timeoutMs = 120_000): Promise<T> {
  const [cmd, ...baseArgs] = cfg.command;
  if (!cmd) return Promise.reject(new Error("engine command is empty"));
  return new Promise<T>((resolve, reject) => {
    const child = spawn(cmd, [...baseArgs, ...args], {
      cwd: cfg.cwd,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      stdio: ["ignore", "pipe", "pipe"],
      shell: process.platform === "win32" && !cmd.endsWith(".exe"),
      windowsHide: true,
    });
    let out = "";
    let err = "";
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`the engine did not answer within ${Math.round(timeoutMs / 1000)} s`));
    }, timeoutMs);
    child.stdout.on("data", (d) => (out += String(d)));
    child.stderr.on("data", (d) => (err += String(d)));
    child.on("error", (e) => {
      clearTimeout(timer);
      reject(e);
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      const line = out.trim().split(/\r?\n/).pop() ?? "";
      if (code !== 0 || !line.startsWith("{")) {
        // The engine's own message is the last informative stderr line (a Python traceback ends with it).
        const msg = err.trim().split(/\r?\n/).filter((l) => l.trim()).pop() ?? `exit code ${code}`;
        reject(new Error(msg.replace(/^\w*Error:\s*/, "")));
        return;
      }
      try {
        resolve(JSON.parse(line) as T);
      } catch (e) {
        reject(new Error(`unreadable engine output: ${(e as Error).message}`));
      }
    });
  });
}
