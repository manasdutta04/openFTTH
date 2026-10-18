/**
 * Spawning a CLI engine without a shell rewriting its arguments. On Windows a bare command is
 * resolved to a file first; a .cmd/.bat launcher (npm shims) still needs cmd.exe, which gets
 * every argument quoted explicitly so MCP config values with spaces and braces survive.
 */

import { spawn, type ChildProcess, type SpawnOptions } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

export function spawnCli(command: string, args: string[], opts: SpawnOptions): ChildProcess {
  const exe = resolveExecutable(command);
  if (/\.(cmd|bat)$/i.test(exe)) {
    return spawn("cmd.exe", ["/d", "/s", "/c", `"${[exe, ...args].map(quoteForCmd).join(" ")}"`], { ...opts, windowsVerbatimArguments: true });
  }
  return spawn(exe, args, opts);
}

/** Find `command` on PATH (Windows only); elsewhere the OS resolves it. */
export function resolveExecutable(command: string): string {
  if (process.platform !== "win32" || /[\\/]/.test(command) || /\.[a-z0-9]+$/i.test(command)) return command;
  const dirs = (process.env.PATH ?? "").split(";").filter(Boolean);
  for (const ext of [".exe", ".cmd", ".bat"]) {
    for (const dir of dirs) {
      const candidate = join(dir, command + ext);
      if (existsSync(candidate)) return candidate;
    }
  }
  return command;
}

/** Quote one argument for a cmd.exe command line (MSVCRT rules; cmd leaves quoted text alone). */
export function quoteForCmd(arg: string): string {
  return `"${arg.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\*)$/, "$1$1")}"`;
}
