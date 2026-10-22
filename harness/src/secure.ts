/**
 * Small security helpers shared by the app API and the MCP link: secrets compared in constant
 * time, and request bodies read with a size cap so a local caller cannot exhaust memory.
 */

import { createHash, timingSafeEqual } from "node:crypto";
import type { IncomingMessage } from "node:http";

/** The largest JSON body either listener accepts (imports pass file paths, not file contents). */
export const MAX_BODY_BYTES = 8 * 1024 * 1024;

/** Whether two secrets are equal, in time that does not depend on where they differ. */
export function sameSecret(given: string | null | undefined, expected: string): boolean {
  if (!given || !expected) return false;
  const a = createHash("sha256").update(given).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

/** The request body, refusing anything over `MAX_BODY_BYTES`. */
export async function readBody(req: IncomingMessage): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > MAX_BODY_BYTES) throw Object.assign(new Error("request body too large"), { status: 413 });
    chunks.push(c as Buffer);
  }
  return Buffer.concat(chunks);
}
