// SPDX-License-Identifier: AGPL-3.0-only
// Reproducible build of the components (L88): one ES module and the theme
// tokens, committed under src/ameesh/ui/static/ so that `pip install`
// delivers the interface without Node (study §5).
//   node scripts/build.mjs [--out DIR]
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as esbuild from "esbuild";

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const at = process.argv.indexOf("--out");
const OUT = at > 0 ? path.resolve(process.argv[at + 1])
  : path.join(ROOT, "..", "src", "ameesh", "ui", "static");

await mkdir(OUT, { recursive: true });
const result = await esbuild.build({
  entryPoints: [path.join(ROOT, "src", "index.ts")],
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: true,
  // no absolute path, no date: the same sources give the same bytes
  sourcemap: false,
  legalComments: "eof",
  banner: { js: "/* @ameesh/ui (L88), SPDX-License-Identifier: AGPL-3.0-only */" },
  charset: "utf8",
  write: false,
  outfile: path.join(OUT, "ameesh-ui.js"),
  logLevel: "warning",
});
for (const file of result.outputFiles) {
  await writeFile(file.path, file.contents);
}
// the tokens alone, for a host page that themes around the components:
// written by the same code the components use (src/tokens.ts)
const theme = await esbuild.build({
  entryPoints: [path.join(ROOT, "src", "tokens.ts")],
  bundle: true, format: "esm", platform: "node", write: false, logLevel: "warning",
});
const url = "data:text/javascript;base64,"
  + Buffer.from(theme.outputFiles[0].contents).toString("base64");
const { tokensSheet } = await import(url);
await writeFile(path.join(OUT, "ameesh-ui.css"),
  "/* SPDX-License-Identifier: AGPL-3.0-only */\n" + tokensSheet() + "\n");
