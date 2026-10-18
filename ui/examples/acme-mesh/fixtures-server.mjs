// SPDX-License-Identifier: AGPL-3.0-only
// Fixtures server of the L92 example (study §2.3): it serves the example page
// with the interface the Python package ships (src/ameesh/ui/static/, the
// same files as `ameesh serve`), plus an ameesh-shaped read API and event
// stream from fixtures.mjs — a fictional organisation, "Acme". No dependency,
// nothing written, the demo token only.
//   node ui/examples/acme-mesh/fixtures-server.mjs [--port 8799] [--host 127.0.0.1]
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { DEMO_TOKEN, ORGANISATION, bearer, resolve } from "./fixtures.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
/** The interface the Python package ships: <repo>/src/ameesh/ui/static/. */
const STATIC = path.join(HERE, "..", "..", "..", "src", "ameesh", "ui", "static");
/** The same strict CSP as the served page, with no frame allowed. */
const HTML_CSP = "default-src 'none'; script-src 'self'; style-src 'self'; "
  + "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
  + "frame-ancestors 'none'";
/** GET only: the example is read-only (the write routes come with L87/L90). */
const FILES = {
  "/": [path.join(HERE, "index.html"), "text/html; charset=utf-8"],
  "/app.mjs": [path.join(HERE, "app.mjs"), "text/javascript; charset=utf-8"],
  "/mount.mjs": [path.join(HERE, "mount.mjs"), "text/javascript; charset=utf-8"],
  "/host.mjs": [path.join(HERE, "host.mjs"), "text/javascript; charset=utf-8"],
  "/fixtures.mjs": [path.join(HERE, "fixtures.mjs"), "text/javascript; charset=utf-8"],
  "/example.css": [path.join(HERE, "example.css"), "text/css; charset=utf-8"],
  "/ui/ameesh-ui.js": [path.join(STATIC, "ameesh-ui.js"), "text/javascript; charset=utf-8"],
  "/ui/ameesh-ui.css": [path.join(STATIC, "ameesh-ui.css"), "text/css; charset=utf-8"],
};
/** A fixture of the approval page: it signs nothing (decision 0012). */
const APPROVAL = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Approval fixture — Acme</title>
<link rel="stylesheet" href="/example.css">
</head>
<body>
<main class="bar">
<h1>Approval fixture</h1>
<p>In a real mesh this page is served by ameesh-approve on the team's origin,
and the human signs with a passkey. This example opens the link but never
signs anything.</p>
<p><a href="/">Back to the example</a></p>
</main>
</body>
</html>
`;

function send(res, status, body, type = "application/json; charset=utf-8") {
  const data = Buffer.isBuffer(body) ? body : Buffer.from(body);
  res.writeHead(status, {
    "Content-Type": type,
    "Content-Length": data.length,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    ...(type.startsWith("text/html") ? { "Content-Security-Policy": HTML_CSP } : {}),
  });
  res.end(data);
}

async function sendFile(res, [file, type]) {
  try {
    send(res, 200, await readFile(file), type);
  } catch {
    send(res, 404, JSON.stringify({ schema: "ameesh-error/1", error: "not_found",
      message: "no such file: " + path.basename(file) }));
  }
}

/** The event stream: a heartbeat, and a `changed` event a view may refresh on. */
function stream(req, res) {
  if (bearer(req.headers.authorization) !== DEMO_TOKEN) {
    return send(res, 401, JSON.stringify({ schema: "ameesh-error/1", error: "unauthorized",
      message: "send Authorization: Bearer " + DEMO_TOKEN }));
  }
  res.writeHead(200, {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
  });
  res.write("retry: 5000\n\n");
  const beat = setInterval(() => { res.write(": heartbeat\n\n"); }, 15000);
  const change = setInterval(() => { res.write("event: changed\ndata: {}\n\n"); }, 30000);
  const stop = () => { clearInterval(beat); clearInterval(change); };
  req.on("close", stop);
  res.on("error", stop);
}

async function handle(req, res) {
  const url = new URL(req.url ?? "/", "http://" + (req.headers.host ?? "127.0.0.1"));
  if (req.method !== "GET") {
    return send(res, 405, JSON.stringify({ schema: "ameesh-error/1", error: "method_not_allowed",
      message: "the example is read-only" }));
  }
  if (url.pathname === "/api/v1/events") return stream(req, res);
  const file = FILES[url.pathname];
  if (file) return sendFile(res, file);
  if (url.pathname === "/a/demo") return send(res, 200, APPROVAL, "text/html; charset=utf-8");
  const api = url.pathname.startsWith("/api/v1/");
  if (api && bearer(req.headers.authorization) !== DEMO_TOKEN) {
    return send(res, 401, JSON.stringify({ schema: "ameesh-error/1", error: "unauthorized",
      message: "send Authorization: Bearer " + DEMO_TOKEN }));
  }
  if (api || url.pathname === "/.well-known/ameesh") {
    const found = resolve(url.pathname);
    if (!found) {
      return send(res, 404, JSON.stringify({ schema: "ameesh-error/1", error: "not_found",
        message: "no fixture for " + url.pathname }));
    }
    return send(res, found[0], JSON.stringify(found[1]));
  }
  send(res, 404, JSON.stringify({ schema: "ameesh-error/1", error: "not_found",
    message: "no fixture for " + url.pathname }));
}

/** The server, not yet listening (tests start it on port 0). */
export function createFixturesServer() {
  return createServer((req, res) => { void handle(req, res); });
}

/** Starts `server`; resolves with its address. */
export function listen(server, { port = 8799, host = "127.0.0.1" } = {}) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, host, () => resolve(server.address()));
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const value = (name, fallback) => {
    const at = process.argv.indexOf(name);
    return at > 0 && process.argv[at + 1] ? process.argv[at + 1] : fallback;
  };
  const port = Number(value("--port", "8799"));
  const host = value("--host", "127.0.0.1");
  const server = createFixturesServer();
  const address = await listen(server, { port, host });
  console.log(`ameesh example (${ORGANISATION}), fixtures only — listening on http://${host}:${address.port}/`);
  console.log(`serving the interface of src/ameesh/ui/static/, token: Bearer ${DEMO_TOKEN}`);
}
