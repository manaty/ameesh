// SPDX-License-Identifier: AGPL-3.0-only
// The host contract (L92): connectHost feeds <ameesh-config> and routes the
// components' events to the host — and nothing else crosses.
import assert from "node:assert/strict";
import { test } from "node:test";
import { settle, ui, window } from "./dom.mjs";

const { document } = window;

function mountConfig() {
  const holder = document.createElement("div");
  const config = document.createElement("ameesh-config");
  config.setAttribute("api-base", "https://mesh.example.org");
  holder.appendChild(config);
  document.body.appendChild(holder);
  return { holder, config };
}

function fakeHost(overrides = {}) {
  const calls = { tokens: [], approvals: [], alerts: [], opened: [] };
  const host = {
    getToken: (mesh) => { calls.tokens.push(mesh); return `tok:${mesh}`; },
    openApproval: (url) => { calls.approvals.push(url); },
    notify: (alert) => { calls.alerts.push(alert); },
    open: (target) => { calls.opened.push(target); },
    ...overrides,
  };
  return { calls, host };
}

const fire = (el, type, detail) =>
  el.dispatchEvent(new window.CustomEvent(type, { detail, bubbles: true, composed: true }));

test("host: meshes() gives the default mesh, theme and the token provider", async () => {
  const { holder, config } = mountConfig();
  const { calls, host } = fakeHost({
    meshes: async () => [{ id: "org", name: "Org", apiBase: "https://mesh.example.org" }],
    theme: "dark",
  });
  const off = ui.connectHost(host, { config });
  await settle();
  assert.equal(config.theme, "dark");
  assert.equal(await config.tokenProvider(), "tok:org");
  assert.deepEqual(calls.tokens, ["org"]);
  off();
  holder.remove();
});

test("host: the components' events reach open, openApproval and notify", () => {
  const { holder, config } = mountConfig();
  const { calls, host } = fakeHost();
  const off = ui.connectHost(host, { config, mesh: "org", apiBase: "https://mesh.example.org" });
  fire(config, "ameesh:open-lot", { lot: 1464, title: "#1464" });
  fire(config, "ameesh:open-project", { project: "ameesh" });
  fire(config, "ameesh:open-agent", { agent: "mty-amh-impl-03", project: "ameesh" });
  fire(config, "ameesh:approval-requested", { url: "https://mesh.example.org/approve/abc" });
  fire(config, "ameesh:alert", { kind: "budget", title: "Budget", text: "cap reached" });
  fire(config, "ameesh:alert", "the mesh is stale");
  assert.deepEqual(calls.opened.map((t) => [t.kind, t.id]),
    [["lot", "1464"], ["project", "ameesh"], ["agent", "mty-amh-impl-03"]]);
  assert.equal(calls.opened[0].mesh, "org");
  assert.deepEqual(calls.approvals, ["https://mesh.example.org/approve/abc"]);
  assert.deepEqual(calls.alerts.map((a) => a.text), ["cap reached", "the mesh is stale"]);
  assert.equal(calls.alerts[0].mesh, "org");
  off();
  holder.remove();
});

test("host: unbinding stops the routing and clears the provider", () => {
  const { holder, config } = mountConfig();
  const { calls, host } = fakeHost();
  const off = ui.connectHost(host, { config });
  off();
  fire(config, "ameesh:alert", "after the unbinding");
  fire(config, "ameesh:open-lot", { lot: 1 });
  assert.deepEqual(calls.alerts, []);
  assert.deepEqual(calls.opened, []);
  assert.equal(config.tokenProvider, undefined);
  holder.remove();
});

test("host: two mesh bindings route each event only to its own config", () => {
  const a = mountConfig(), b = mountConfig();
  const { calls, host } = fakeHost();
  const offA = ui.connectHost(host, { config: a.config, mesh: "a" });
  const offB = ui.connectHost(host, { config: b.config, mesh: "b" });
  fire(a.config, "ameesh:open-lot", { lot: 1 });
  fire(b.config, "ameesh:alert", "only b");
  fire(a.config, "ameesh:approval-requested", "https://a.example/a/1");
  assert.deepEqual(calls.opened.map((t) => t.mesh), ["a"]);
  assert.deepEqual(calls.alerts.map((t) => t.mesh), ["b"]);
  assert.deepEqual(calls.approvals, ["https://a.example/a/1"]);
  offA(); offB();
  a.holder.remove(); b.holder.remove();
});

test("host: a <ameesh-config> is created when the page has none", () => {
  for (const el of document.querySelectorAll("ameesh-config")) el.remove();
  const holder = document.createElement("div");
  document.body.appendChild(holder);
  const { host } = fakeHost();
  const off = ui.connectHost(host, { root: holder, apiBase: "https://m.example" });
  const config = holder.querySelector("ameesh-config");
  assert.ok(config);
  assert.equal(config.apiBase, "https://m.example");
  off();
  holder.remove();
});

test("host: with a created <ameesh-config>, the events of the components beside it reach the host", () => {
  for (const el of document.querySelectorAll("ameesh-config")) el.remove();
  const holder = document.createElement("div");
  const view = document.createElement("div");
  holder.appendChild(view);
  document.body.appendChild(holder);
  const { host, calls } = fakeHost();
  const off = ui.connectHost(host, { root: holder });
  fire(view, "ameesh:approval-requested", "https://m.example/a/1");
  assert.deepEqual(calls.approvals, ["https://m.example/a/1"]);
  off();
  fire(view, "ameesh:approval-requested", "https://m.example/a/2");
  assert.deepEqual(calls.approvals, ["https://m.example/a/1"]);
  holder.remove();
});

test("host: an explicit API base selects the token of that mesh", async () => {
  const { holder, config } = mountConfig();
  config.apiBase = "https://b.example";
  const { host } = fakeHost({ meshes: () => [
    { id: "a", apiBase: "https://a.example" },
    { id: "b", apiBase: "https://b.example" },
  ] });
  const off = ui.connectHost(host, { config });
  await settle();
  assert.equal(await config.tokenProvider(), "tok:b");
  off(); holder.remove();
});

test("host: pending mesh discovery does not mutate an unbound config", async () => {
  const { holder, config } = mountConfig();
  config.apiBase = "";
  let resolve;
  const { host } = fakeHost({ meshes: () => new Promise((done) => { resolve = done; }) });
  const off = ui.connectHost(host, { config });
  off();
  resolve([{ id: "a", apiBase: "https://a.example" }]);
  await settle();
  assert.equal(config.apiBase, "");
  holder.remove();
});
