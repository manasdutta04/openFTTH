/**
 * The crew manifest (`crews/<name>/crew.openftth.json`, format `openftth.crew` v1) and each
 * folk's instructions and memory.
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export interface CrewMember {
  key: string;
  name: string;
  title: string;
  instructions: string; // markdown text (resolved from the manifest's file path)
  tools: string[]; // engine tool names this folk may call without a card
  color?: string;
  engine?: { kind?: string; command?: string; args?: string[]; model?: string };
}

export interface Crew {
  name: string;
  description: string;
  members: CrewMember[];
  room: { name: string; bulletin: string; defaultResponder: string };
  dir: string;
}

export function loadCrew(manifestPath: string): Crew {
  const path = resolve(manifestPath);
  const raw = JSON.parse(readFileSync(path, "utf8")) as Record<string, any>;
  if (raw.format !== "openftth.crew" || raw.version !== 1) {
    throw new Error(`unsupported crew manifest ${path}: format=${raw.format} version=${raw.version}`);
  }
  const crew = raw.crew as Record<string, any>;
  const dir = dirname(path);
  const members: CrewMember[] = (crew.members as Array<Record<string, any>>).map((m) => {
    for (const k of ["key", "name", "title", "instructions"]) {
      if (typeof m[k] !== "string" || !m[k]) throw new Error(`crew member missing ${k}`);
    }
    if (!/^[a-z][a-z0-9_-]{0,31}$/.test(m.key)) throw new Error(`invalid member key ${m.key}`);
    const instrPath = resolve(dir, m.instructions);
    if (!existsSync(instrPath)) throw new Error(`instructions file not found: ${instrPath}`);
    return {
      key: m.key,
      name: m.name,
      title: m.title,
      instructions: readFileSync(instrPath, "utf8"),
      tools: Array.isArray(m.tools) ? (m.tools as string[]) : [],
      ...(m.color ? { color: String(m.color) } : {}),
      ...(m.engine ? { engine: m.engine } : {}),
    };
  });
  const keys = new Set(members.map((m) => m.key));
  if (keys.size !== members.length) throw new Error("duplicate member keys");
  const room = crew.room ?? {};
  const defaultResponder = String(room.defaultResponder ?? members[0]?.key ?? "");
  if (!keys.has(defaultResponder)) throw new Error(`defaultResponder ${defaultResponder} is not a member`);
  return {
    name: String(crew.name ?? "crew"),
    description: String(crew.description ?? ""),
    members,
    room: { name: String(room.name ?? "Room"), bulletin: String(room.bulletin ?? ""), defaultResponder },
    dir,
  };
}

/** Find `@Name` mentions of crew members in a text, in order, without duplicates. */
export function mentions(crew: Crew, text: string): CrewMember[] {
  const out: CrewMember[] = [];
  for (const m of crew.members) {
    const re = new RegExp(`(^|[^\\w])@${escapeRe(m.name)}\\b`, "i");
    if (re.test(text) && !out.includes(m)) out.push(m);
  }
  return out;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export const MEMORY_MAX_CHARS = 24_000;

export class FolkMemory {
  private readonly dataDir: string;

  constructor(dataDir: string) {
    this.dataDir = dataDir;
  }

  path(key: string): string {
    return join(this.dataDir, "folks", key, "MEMORY.md");
  }

  read(key: string): string {
    const p = this.path(key);
    if (!existsSync(p)) return "";
    const text = readFileSync(p, "utf8");
    return text.length > MEMORY_MAX_CHARS ? text.slice(0, MEMORY_MAX_CHARS) + "\n…(truncated)" : text;
  }

  write(key: string, text: string): void {
    const p = this.path(key);
    mkdirSync(dirname(p), { recursive: true });
    writeFileSync(p, text, "utf8");
  }

  append(key: string, line: string): void {
    const current = existsSync(this.path(key)) ? readFileSync(this.path(key), "utf8") : "";
    this.write(key, current + (current.endsWith("\n") || !current ? "" : "\n") + line + "\n");
  }
}

/** The system prompt a folk runs with. Stable per folk so the engine process can be reused. */
export function systemPrompt(crew: Crew, member: CrewMember, memory: string): string {
  const roster = crew.members
    .map((m) => `- @${m.name} (${m.key}): ${m.title}`)
    .join("\n");
  return [
    `You are ${member.name}, the "${member.title}" folk of the ${crew.name}.`,
    "",
    member.instructions.trim(),
    "",
    "## Room",
    crew.room.bulletin.trim(),
    "",
    "## Crew",
    roster,
    "Hand work to a teammate by mentioning them with @Name in your reply; keep the hand-off message self-contained.",
    "",
    "## Hard rules",
    "- Never state a number that a tool did not return. Quote decisions[].reason when explaining.",
    "- If a tool returns error=approval_required, explain the step and its cost and ask the user to approve; do not retry it.",
    memory.trim() ? `\n## Your memory\n${memory.trim()}` : "",
  ].join("\n");
}
