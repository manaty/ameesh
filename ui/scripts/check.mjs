// SPDX-License-Identifier: AGPL-3.0-only
// CI checks of the committed build (L88, study §5):
// - the client types match docs/api/openapi-v1.json;
// - the build is reproducible: rebuilding gives the committed bytes;
// - the whole stays under the size budget, compressed.
import { execFileSync } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

export const BUDGET = 80 * 1024;
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const STATIC = path.join(ROOT, "..", "src", "ameesh", "ui", "static");
const FILES = ["ameesh-ui.js", "ameesh-ui.css", "index.html", "page.css"];
const BUILT = ["ameesh-ui.js", "ameesh-ui.css"];

let failed = false;
const fail = (msg) => { console.error("ui check: " + msg); failed = true; };

try {
  execFileSync(process.execPath, [path.join(ROOT, "scripts", "gen-api.mjs"), "--check"],
               { stdio: "inherit" });
} catch {
  fail("client types stale");
}

const tmp = await mkdtemp(path.join(process.env.TMPDIR || os.tmpdir(), "ameesh-ui-"));
try {
  execFileSync(process.execPath, [path.join(ROOT, "scripts", "build.mjs"), "--out", tmp],
               { stdio: "inherit" });
  for (const name of BUILT) {
    const [fresh, committed] = await Promise.all([
      readFile(path.join(tmp, name)), readFile(path.join(STATIC, name)).catch(() => null)]);
    if (committed === null || !fresh.equals(committed)) {
      fail(`${name}: the committed build is not the build of the sources (run \`npm run build\`)`);
    }
  }
} finally {
  await rm(tmp, { recursive: true, force: true });
}

let total = 0;
for (const name of FILES) {
  const data = await readFile(path.join(STATIC, name)).catch(() => null);
  if (data === null) {
    fail(`${name} missing`);
    continue;
  }
  const size = gzipSync(data, { level: 9 }).length;
  total += size;
  console.log("%s  %d bytes, %d compressed", name, data.length, size);
}
console.log("total %d bytes compressed, budget %d", total, BUDGET);
if (total > BUDGET) fail(`over budget: ${total} > ${BUDGET} bytes compressed`);
process.exit(failed ? 1 : 0);
