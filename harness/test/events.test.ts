import { strict as assert } from "node:assert";
import { test } from "node:test";
import { EventBus, encode, parseSse } from "../src/events.ts";

test("frames get sequential ids and replay resumes after a cursor", () => {
  const bus = new EventBus();
  const a = bus.emit("message", { n: 1 });
  const b = bus.emit("message", { n: 2 });
  bus.emit("ping", {}); // pings are not buffered
  const c = bus.emit("folk", { key: "planner", state: "idle" });
  assert.equal(a.seq, 1);
  assert.equal(c.seq, 4);
  assert.equal(a.id, `${bus.streamId}:1`);
  const { frames, resumed } = bus.replay(b.id);
  assert.equal(resumed, true);
  assert.deepEqual(frames.map((f) => f.seq), [4]);
  const other = bus.replay("otherstream:3");
  assert.equal(other.resumed, false);
  assert.equal(other.frames.length, 3);
});

test("SSE encoding round-trips through the parser", () => {
  const bus = new EventBus();
  const f = bus.emit("tool.activity", { folk: "planner", tool: "cluster_homes", state: "started" });
  const parsed = parseSse(encode(f));
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0]!.event, "tool.activity");
  assert.equal(parsed[0]!.id, f.id);
  assert.deepEqual(parsed[0]!.data, { folk: "planner", tool: "cluster_homes", state: "started" });
});
