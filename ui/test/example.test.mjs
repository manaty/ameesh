// SPDX-License-Identifier: AGPL-3.0-only
// L92 proof: the example application opens the fictional mesh — the page's
// markup, the host contract and the fixtures — and its fixtures server answers
// like a mesh (401 without the token, discovery, read routes, 404).
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { fakeFetch, settle, ui, window } from "./dom.mjs";
import { DEMO_TOKEN, MESH_ID, ORGANISATION, PROJECT, routeMap } from "../examples/acme-mesh/fixtures.mjs";
import { mount } from "../examples/acme-mesh/mount.mjs";
import { createFixturesServer, listen } from "../examples/acme-mesh/fixtures-server.mjs";

const { document } = window;
/** The real fetch, before the page tests stub the global one. */
const httpFetch = globalThis.fetch;
const EXAMPLE = new URL("../examples/acme-mesh/", import.meta.url);
const pageSource = await readFile(new URL("index.html", EXAMPLE), "utf8");
const body = /<body[^>]*>([\s\S]*)<\/body>/.exec(pageSource)[1];

const text = (el) => el.shadowRoot.textContent.replace(/\s+/g, " ").trim();

/** The screen the app mounted, settled and read through its own shadow root. */
async function screen(app, name) {
  const el = app.shadowRoot.querySelector(name);
  await settle(el);
  return text(el);
}

/** Opens the example page as a browser would: markup, then mount. */
let unbind = null;
async function open() {
  unbind?.();                       // one binding at a time, like one page load
  unbind = null;
  document.body.innerHTML = body;
  const seen = [];
  const fetch = fakeFetch(routeMap());
  globalThis.fetch = fetch;
  window.fetch = fetch;
  const mounted = mount(ui, { window, status: (line) => seen.push(line) });
  unbind = mounted.off;
  await settle(mounted.config);
  const app = document.querySelector("ameesh-app");
  await settle(app);
  return { host: mounted.host, config: mounted.config, app, fetch, seen };
}

test("example: the page opens Acme through the host contract", async () => {
  const { config, app, fetch, seen } = await open();
  assert.equal(config.apiBase, window.location.origin);
  assert.match(text(app), /Connected to Acme/);
  assert.match(await screen(app, "ameesh-projects"), new RegExp(PROJECT));
  assert.match(await screen(app, "ameesh-projects"), /ada/);
  const authorized = fetch.calls.filter((call) => call.headers.get("Authorization"));
  assert.ok(authorized.length, "requests carry a token");
  assert.equal(authorized[0].headers.get("Authorization"), "Bearer " + DEMO_TOKEN);
  assert.ok(seen.some((line) => line.includes(`token requested for mesh ${MESH_ID}`)));
});

test("example: opening a project shows the timeline of its lots", async () => {
  const { app, fetch } = await open();
  app.dispatchEvent(new window.CustomEvent("ameesh:open-project",
    { detail: { project: PROJECT }, bubbles: true, composed: true }));
  await settle(app);
  const timeline = await screen(app, "ameesh-timeline");
  assert.match(timeline, /#12 Panier d'achat/);
  assert.match(timeline, /#18 Facturation du portail/);
  assert.ok(fetch.calls.some((call) => call.path === `/api/v1/projects/${PROJECT}/progress`));
  assert.equal(window.location.hash, `#/project/${PROJECT}`);
});

test("example: the components' events reach the host object", async () => {
  const { host, config } = await open();
  const approvals = [];
  const alerts = [];
  host.openApproval = (url) => approvals.push(url);
  host.notify = (alert) => alerts.push(alert);
  const fire = (type, detail) => config.dispatchEvent(
    new window.CustomEvent(type, { detail, bubbles: true, composed: true }));
  fire("ameesh:approval-requested", { url: "https://mesh.acme.example/a/demo" });
  fire("ameesh:alert", { kind: "demo", title: "Acme", text: "waiting for a verdict" });
  assert.deepEqual(approvals, ["https://mesh.acme.example/a/demo"]);
  assert.equal(alerts.length, 1);
  assert.equal(alerts[0].text, "waiting for a verdict");
  assert.equal(alerts[0].mesh, MESH_ID);
});

test("example: the fixtures server answers like a mesh", async () => {
  const server = createFixturesServer();
  const address = await listen(server, { port: 0 });
  const base = `http://127.0.0.1:${address.port}`;
  const auth = { headers: { Authorization: "Bearer " + DEMO_TOKEN } };
  try {
    const page = await httpFetch(base + "/");
    assert.equal(page.status, 200);
    assert.match(page.headers.get("content-security-policy"), /frame-ancestors 'none'/);
    assert.match(await page.text(), /<ameesh-config>/);

    // the interface the page imports is the one the Python package serves
    const module = await httpFetch(base + "/ui/ameesh-ui.js");
    assert.equal(module.status, 200);
    assert.match(module.headers.get("content-type"), /javascript/);
    assert.match(await module.text(), /connectHost/);

    const denied = await httpFetch(base + "/api/v1/projects");
    assert.equal(denied.status, 401);
    assert.equal((await denied.json()).error, "unauthorized");

    const discovery = await httpFetch(base + "/.well-known/ameesh");
    assert.equal(discovery.status, 200);
    assert.equal((await discovery.json()).organisation, ORGANISATION);

    const projects = await (await httpFetch(base + "/api/v1/projects", auth)).json();
    assert.equal(projects.projects[0].name, PROJECT);
    const agent = await (await httpFetch(base + "/api/v1/agents/ada", auth)).json();
    assert.equal(agent.agent.key_ready, true);

    const missing = await httpFetch(base + "/api/v1/nowhere", auth);
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).error, "not_found");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("example: every module parses, Electron reference included", () => {
  for (const name of ["app.mjs", "host.mjs", "mount.mjs", "fixtures.mjs",
    "fixtures-server.mjs", "electron/main.mjs", "electron/preload.mjs",
    "electron/renderer.mjs"]) {
    execFileSync(process.execPath, ["--check", fileURLToPath(new URL(name, EXAMPLE))],
      { stdio: "pipe" });
  }
});

test("example: the guides the package README announces exist", async () => {
  const readme = await readFile(new URL("../README.md", import.meta.url), "utf8");
  for (const guide of ["docs/ui/CONTRAT-HOTE.md", "docs/ui/GUIDE-ELECTRON.md",
    "docs/ui/GUIDE-PAGE-WEB.md"]) {
    assert.ok(readme.includes(guide), guide + " is announced by ui/README.md");
    assert.ok((await readFile(new URL("../../" + guide, import.meta.url), "utf8")).length > 0);
  }
});
