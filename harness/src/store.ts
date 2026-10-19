/**
 * Persistence: messages in SQLite (node:sqlite), small records as atomically written JSON,
 * runtime events as NDJSON (the Backstage log).
 */

import { DatabaseSync } from "node:sqlite";
import { appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";

export type Role = "user" | "folk" | "system";
/** `steer` is the engineer's own change made through the app (a moved site, an excluded building, a no-dig street). */
export type MessageKind = "text" | "tool" | "approval" | "handoff" | "error" | "steer";

export interface Message {
  id: string;
  room: string;
  at: number;
  role: Role;
  folk: string | null;
  kind: MessageKind;
  text: string;
  json: Record<string, unknown> | null;
}

export interface RuntimeEvent {
  at: number;
  folk: string | null;
  event: string;
  detail: unknown;
}

export class Store {
  readonly db: DatabaseSync;
  readonly dir: string;

  constructor(dataDir: string) {
    this.dir = dataDir;
    mkdirSync(dataDir, { recursive: true });
    this.db = new DatabaseSync(join(dataDir, "messages.db"));
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY,
        room TEXT NOT NULL,
        at INTEGER NOT NULL,
        role TEXT NOT NULL,
        folk TEXT,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        json TEXT
      );
      CREATE INDEX IF NOT EXISTS messages_room_at ON messages(room, at);
      CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    `);
  }

  addMessage(input: Omit<Message, "id" | "at"> & { id?: string; at?: number }): Message {
    const msg: Message = { id: input.id ?? randomUUID(), at: input.at ?? Date.now(), ...input } as Message;
    this.db
      .prepare("INSERT INTO messages (id, room, at, role, folk, kind, text, json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
      .run(msg.id, msg.room, msg.at, msg.role, msg.folk, msg.kind, msg.text, msg.json ? JSON.stringify(msg.json) : null);
    return msg;
  }

  updateMessageText(id: string, text: string, json?: Record<string, unknown> | null): void {
    if (json === undefined) {
      this.db.prepare("UPDATE messages SET text = ? WHERE id = ?").run(text, id);
    } else {
      this.db.prepare("UPDATE messages SET text = ?, json = ? WHERE id = ?").run(text, json ? JSON.stringify(json) : null, id);
    }
  }

  messages(room: string, since = 0, limit = 200): Message[] {
    const rows = this.db
      .prepare("SELECT * FROM messages WHERE room = ? AND at > ? ORDER BY at ASC, rowid ASC LIMIT ?")
      .all(room, since, limit) as Array<Record<string, unknown>>;
    return rows.map((r) => ({
      id: String(r.id),
      room: String(r.room),
      at: Number(r.at),
      role: r.role as Role,
      folk: (r.folk as string | null) ?? null,
      kind: r.kind as MessageKind,
      text: String(r.text),
      json: r.json ? (JSON.parse(String(r.json)) as Record<string, unknown>) : null,
    }));
  }

  getKv<T>(key: string, fallback: T): T {
    const row = this.db.prepare("SELECT value FROM kv WHERE key = ?").get(key) as { value: string } | undefined;
    return row ? (JSON.parse(row.value) as T) : fallback;
  }

  setKv(key: string, value: unknown): void {
    this.db.prepare("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value").run(key, JSON.stringify(value));
  }

  /** Forget a room's conversation (a project reset). */
  deleteRoom(room: string): void {
    this.db.prepare("DELETE FROM messages WHERE room = ?").run(room);
  }

  /** Forget a project's runtime log (a project reset). */
  clearRuntime(project: string): void {
    const p = join(this.dir, "backstage", `${project}.ndjson`);
    if (existsSync(p)) rmSync(p);
  }

  appendRuntime(project: string, ev: RuntimeEvent): void {
    const p = join(this.dir, "backstage", `${project}.ndjson`);
    mkdirSync(dirname(p), { recursive: true });
    appendFileSync(p, JSON.stringify(ev) + "\n", "utf8");
  }

  readRuntime(project: string, limit = 200): RuntimeEvent[] {
    const p = join(this.dir, "backstage", `${project}.ndjson`);
    if (!existsSync(p)) return [];
    const lines = readFileSync(p, "utf8").split("\n").filter(Boolean);
    return lines.slice(-limit).map((l) => JSON.parse(l) as RuntimeEvent);
  }

  close(): void {
    this.db.close();
  }
}

/** Write JSON atomically (temp file + rename) so a crash never leaves a half-written record. */
export function writeJsonAtomic(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const tmp = `${path}.${process.pid}.${Date.now()}.tmp`;
  writeFileSync(tmp, JSON.stringify(value, null, 2) + "\n", "utf8");
  renameSync(tmp, path);
}

export function readJsonOr<T>(path: string, fallback: T): T {
  if (!existsSync(path)) return fallback;
  return JSON.parse(readFileSync(path, "utf8")) as T;
}
