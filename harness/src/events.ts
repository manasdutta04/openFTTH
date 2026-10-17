/**
 * The live event bus and its SSE encoding.
 *
 * Every frame gets `id: <stream>:<seq>`. A replay buffer lets a client resume with
 * `Last-Event-ID` (or `?since=`) after a reconnect; the first frame is always `hello`.
 */

import type { ServerResponse } from "node:http";
import { randomBytes } from "node:crypto";

export type FrameKind =
  | "hello"
  | "ping"
  | "message"
  | "message.patch"
  | "tool.activity"
  | "approval"
  | "design.version"
  | "design.stage"
  | "runtime"
  | "folk"
  | "project";

export interface Frame {
  id: string;
  seq: number;
  kind: FrameKind;
  data: unknown;
  at: number;
}

export const REPLAY_LIMIT = 500;
export const PING_MS = 15_000;

export class EventBus {
  readonly streamId = randomBytes(4).toString("hex");
  private seq = 0;
  private readonly buffer: Frame[] = [];
  private readonly clients = new Set<ServerResponse>();
  private readonly listeners = new Set<(f: Frame) => void>();
  private pingTimer: NodeJS.Timeout | null = null;

  emit(kind: FrameKind, data: unknown): Frame {
    this.seq += 1;
    const frame: Frame = { id: `${this.streamId}:${this.seq}`, seq: this.seq, kind, data, at: Date.now() };
    if (kind !== "ping") {
      this.buffer.push(frame);
      if (this.buffer.length > REPLAY_LIMIT) this.buffer.shift();
    }
    const text = encode(frame);
    for (const res of this.clients) {
      res.write(text);
    }
    for (const l of this.listeners) l(frame);
    return frame;
  }

  on(listener: (f: Frame) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  get cursor(): string {
    return `${this.streamId}:${this.seq}`;
  }

  /** Frames after `since` (an id like `stream:seq`). Unknown stream → full buffer, resumed=false. */
  replay(since: string | undefined): { frames: Frame[]; resumed: boolean } {
    if (!since) return { frames: [], resumed: false };
    const [stream, seqText] = since.split(":");
    const seq = Number(seqText);
    if (stream !== this.streamId || !Number.isFinite(seq)) return { frames: this.buffer.slice(), resumed: false };
    return { frames: this.buffer.filter((f) => f.seq > seq), resumed: true };
  }

  subscribe(res: ServerResponse, since: string | undefined, opts: { screens?: boolean } = {}): void {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    });
    const { frames, resumed } = this.replay(since);
    res.write(encode({ id: this.cursor, seq: this.seq, kind: "hello", data: { cursor: this.cursor, resumed, replayed: frames.length, screens: opts.screens ?? true }, at: Date.now() }));
    for (const f of frames) res.write(encode(f));
    this.clients.add(res);
    this.ensurePing();
    res.on("close", () => {
      this.clients.delete(res);
      if (this.clients.size === 0 && this.pingTimer) {
        clearInterval(this.pingTimer);
        this.pingTimer = null;
      }
    });
  }

  get clientCount(): number {
    return this.clients.size;
  }

  close(): void {
    for (const res of this.clients) res.end();
    this.clients.clear();
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  private ensurePing(): void {
    if (this.pingTimer) return;
    this.pingTimer = setInterval(() => this.emit("ping", { at: Date.now() }), PING_MS);
    this.pingTimer.unref();
  }
}

export function encode(frame: Frame): string {
  return `id: ${frame.id}\nevent: ${frame.kind}\ndata: ${JSON.stringify(frame.data)}\n\n`;
}

/** Parse SSE text into frames (used by tests and the Rust client's reference implementation). */
export function parseSse(text: string): Array<{ id?: string; event?: string; data: unknown }> {
  const out: Array<{ id?: string; event?: string; data: unknown }> = [];
  for (const block of text.split("\n\n")) {
    if (!block.trim()) continue;
    const frame: { id?: string; event?: string; data: unknown } = { data: undefined };
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("id: ")) frame.id = line.slice(4);
      else if (line.startsWith("event: ")) frame.event = line.slice(7);
      else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
    }
    if (dataLines.length) {
      try {
        frame.data = JSON.parse(dataLines.join("\n"));
      } catch {
        frame.data = dataLines.join("\n");
      }
    }
    out.push(frame);
  }
  return out;
}
