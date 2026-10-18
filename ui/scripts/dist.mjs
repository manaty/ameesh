// SPDX-License-Identifier: AGPL-3.0-only
// Builds the publishable npm package (L92, study §2.2): the same sources as
// the committed build served by `ameesh serve`, but as an ES module for the
// host's bundler — `lit` stays external (the host resolves it from the
// package's dependency) — plus the type declarations. Nothing is published:
// `npm pack` shows what would be.
//   node scripts/dist.mjs
import { execFileSync } from "node:child_process";
import { cp, mkdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DIST = path.join(ROOT, "dist");

await rm(DIST, { recursive: true, force: true });
await mkdir(DIST, { recursive: true });

await esbuild.build({
  entryPoints: [path.join(ROOT, "src", "index.ts"), path.join(ROOT, "src", "tokens.ts")],
  bundle: true,
  format: "esm",
  target: "es2022",
  platform: "neutral",
  // the host's bundler provides lit; publishing it twice would give two
  // registries of custom elements
  external: ["lit", "lit/*"],
  outdir: DIST,
  // no absolute path, no date: the same sources give the same bytes
  sourcemap: false,
  minify: false,
  legalComments: "eof",
  banner: { js: "/* @ameesh/ui, SPDX-License-Identifier: AGPL-3.0-only */" },
  charset: "utf8",
  logLevel: "warning",
});

// the declarations, from the tsconfig the typecheck uses
execFileSync(path.join(ROOT, "node_modules", ".bin", "tsc"),
  ["-p", path.join(ROOT, "tsconfig.json"), "--declaration", "--emitDeclarationOnly",
    "--outDir", DIST],
  { stdio: "inherit", cwd: ROOT });

// npm ships a LICENSE found at the package root; the repository's own text
await cp(path.join(ROOT, "..", "LICENSE"), path.join(ROOT, "LICENSE"));
