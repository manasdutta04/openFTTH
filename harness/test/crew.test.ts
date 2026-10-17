import { strict as assert } from "node:assert";
import { join } from "node:path";
import { test } from "node:test";
import { findRepoRoot } from "../src/config.ts";
import { loadCrew, mentions, systemPrompt } from "../src/crew.ts";

const manifest = join(findRepoRoot(), "crews", "openftth", "crew.openftth.json");

test("the shipped crew manifest loads with four folks and tool lists", () => {
  const crew = loadCrew(manifest);
  assert.deepEqual(crew.members.map((m) => m.key), ["surveyor", "planner", "checker", "drafter"]);
  assert.equal(crew.room.defaultResponder, "planner");
  const planner = crew.members[1]!;
  assert.ok(planner.tools.includes("cluster_homes"));
  assert.ok(!planner.tools.includes("add_trench"), "risky tools are never allow-listed");
  assert.ok(!planner.tools.includes("check_rules"), "planner must not validate itself");
  assert.match(planner.instructions, /Planner/);
});

test("mentions find @Name case-insensitively and in order", () => {
  const crew = loadCrew(manifest);
  const found = mentions(crew, "Done. @checker please validate, then @Drafter reports. Email me@example.com");
  assert.deepEqual(found.map((m) => m.key), ["checker", "drafter"]);
  assert.deepEqual(mentions(crew, "no mentions here"), []);
});

test("system prompt holds the instructions, roster, rules and memory", () => {
  const crew = loadCrew(manifest);
  const text = systemPrompt(crew, crew.members[2]!, "Client X wants 12-fibre drops.");
  assert.match(text, /You are Checker/);
  assert.match(text, /@Planner \(planner\)/);
  assert.match(text, /Never state a number that a tool did not return/);
  assert.match(text, /12-fibre drops/);
});
