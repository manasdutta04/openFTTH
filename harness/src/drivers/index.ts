/** Pick the driver for a folk from the active engine kind. */

import { claudeDriverFactory } from "./claude.ts";
import { hermesDriverFactory } from "./acp.ts";
import { codexDriverFactory } from "./codex.ts";
import { openAiDriverFactory } from "./openai.ts";
import type { Driver, FolkProcessSpec } from "./types.ts";

export function selectDriver(spec: FolkProcessSpec): Driver {
  switch (spec.engine.kind) {
    case "claude":
      return claudeDriverFactory(spec);
    case "codex":
      return codexDriverFactory(spec);
    case "hermes":
      return hermesDriverFactory(spec);
    case "ollama":
    case "openrouter":
    case "openai":
      return openAiDriverFactory(spec);
    default:
      throw new Error(`no driver for engine kind "${(spec.engine as { kind: string }).kind}"`);
  }
}
