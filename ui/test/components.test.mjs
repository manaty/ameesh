// SPDX-License-Identifier: AGPL-3.0-only
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { fakeFetch, settle, ui, violations, window } from "./dom.mjs";

const { document } = window;
const DISCOVERY = { schema: "ameesh-discovery/1", organisation: "Acme", api: "/api/v1",
  versions: ["v1"], version: "1.2.3", mode: "local", oidc_issuer: null, approve_url: null,
  languages: ["fr", "en"], openapi: "/api/v1/openapi.json" };
const ME = { schema: "ameesh-me/1", human: "human:ada", language: "fr", mode: "local",
  organisation: "Acme", roles: [], deputyships: [] };

function text(el) {
  return el.shadowRoot.textContent.replace(/\s+/g, " ").trim();
}

beforeEach(() => {
  document.body.replaceChildren();
  document.documentElement.lang = "en";
});

async function mount(html, { routes, token = "tok" } = {}) {
  const fetch = fakeFetch(routes ?? {
    "/.well-known/ameesh": [200, DISCOVERY], "/api/v1/me": [200, ME] });
  const holder = document.createElement("div");
  holder.innerHTML = html; // test fixture only, never in the components
  const config = holder.querySelector("ameesh-config");
  const app = holder.querySelector("ameesh-app");
  if (app && !config) app.client = ui.createClient({ baseUrl: "http://127.0.0.1:8780", tokenProvider: () => token, fetch });
  if (config && token) config.tokenProvider = () => token;
  window.fetch = fetch;
  globalThis.fetch = fetch;
  document.body.append(...holder.childNodes);
  await settle(config);
  await settle(app);
  return { config, app, fetch };
}

test("app: who, which mesh, in the document's language, accessible", async () => {
  const { app, fetch } = await mount(
    '<ameesh-config api-base="http://127.0.0.1:8780"><ameesh-app></ameesh-app></ameesh-config>');
  assert.match(text(app), /Connected to Acme/);
  assert.match(text(app), /Mesh 1\.2\.3, local mode/);
  assert.equal(fetch.calls.find((c) => c.path === "/api/v1/me").headers.get("Authorization"), "Bearer tok");
  assert.deepEqual(await violations(), []);
});

test("app: the element's lang wins, fr", async () => {
  const { app } = await mount('<div lang="fr"><ameesh-app></ameesh-app></div>');
  assert.match(text(app), /Connecté à Acme/);
  assert.equal(app.shadowRoot.querySelector("nav").getAttribute("aria-label"), "Rubriques");
});

test("app standalone: the human's language becomes the page's", async () => {
  const { app } = await mount("<ameesh-app standalone></ameesh-app>");
  assert.equal(document.documentElement.lang, "fr");
  assert.match(text(app), /Vue d'ensemble/);
});

test("app: a refused token is said, with an event and a retry", async () => {
  const events = [];
  document.addEventListener("ameesh:unauthorized", (e) => events.push(e.type), { once: true });
  const { app } = await mount("<ameesh-app></ameesh-app>", { routes: {
    "/.well-known/ameesh": [200, DISCOVERY],
    "/api/v1/me": [401, { schema: "ameesh-error/1", error: "unauthorized", message: "no" }] } });
  const alert = app.shadowRoot.querySelector('[role="alert"]');
  assert.match(alert.textContent, /Token missing or refused/);
  assert.ok(alert.querySelector("button"));
  assert.deepEqual(events, ["ameesh:unauthorized"]);
  assert.deepEqual(await violations(), []);
});

test("app: texts from the mesh are rendered as text, never as HTML", async () => {
  const { app } = await mount("<ameesh-app></ameesh-app>", { routes: {
    "/.well-known/ameesh": [200, { ...DISCOVERY, organisation: "<img src=x onerror=alert(1)>" }],
    "/api/v1/me": [200, { ...ME, language: "en" }] } });
  assert.equal(app.shadowRoot.querySelector("img"), null);
  assert.match(text(app), /<img src=x/);
});

test("config token-form: asks, keeps in memory, then shows the app", async () => {
  const { config, app, fetch } = await mount(
    '<ameesh-config token-form><ameesh-app standalone></ameesh-app></ameesh-config>', { token: null });
  const form = config.shadowRoot.querySelector("form");
  assert.ok(form, "form shown");
  assert.match(config.shadowRoot.textContent, /~\/\.config\/ameesh\/serve-token/);
  assert.deepEqual(await violations(), []);
  const input = config.shadowRoot.querySelector("input");
  input.value = "  typed-token  ";
  form.dispatchEvent(new window.SubmitEvent("submit", { cancelable: true }));
  await settle(config);
  await settle(app);
  assert.equal(config.shadowRoot.querySelector("form"), null);
  assert.equal(input.value, "");
  const last = fetch.calls.filter((c) => c.path === "/api/v1/me").at(-1);
  assert.equal(last.headers.get("Authorization"), "Bearer typed-token");
  assert.match(text(app), /Connecté à Acme/);
  assert.equal(window.localStorage.length + window.sessionStorage.length, 0);
  config.disconnect();
  await settle(config);
  assert.ok(config.shadowRoot.querySelector("form"));
});

test("config token-form: a refused token is forgotten and asked again", async () => {
  const { config, app, fetch } = await mount(
    '<ameesh-config token-form><ameesh-app standalone></ameesh-app></ameesh-config>', { token: null,
      routes: { "/.well-known/ameesh": [200, DISCOVERY],
                "/api/v1/me": [401, { schema: "ameesh-error/1", error: "unauthorized", message: "no" }] } });
  const form = config.shadowRoot.querySelector("form");
  config.shadowRoot.querySelector("input").value = "wrong-token";
  form.dispatchEvent(new window.SubmitEvent("submit", { cancelable: true }));
  await settle(config);
  await settle(app);
  await settle(config);
  assert.equal(config.connected, false);
  assert.ok(config.shadowRoot.querySelector("form"), "form shown again");
  assert.match(config.shadowRoot.querySelector('[role="alert"]').textContent, /Token missing or refused/);
  const sent = fetch.calls.filter((c) => c.path === "/api/v1/me").map((c) => c.headers.get("Authorization"));
  // after the refusal, one reload without the token, then nothing: no loop
  assert.deepEqual(sent.slice(sent.indexOf("Bearer wrong-token")), ["Bearer wrong-token", null]);
  await settle(app);
  assert.equal(fetch.calls.filter((c) => c.path === "/api/v1/me").length, sent.length);
  await app.refresh();
  const last = fetch.calls.filter((c) => c.path === "/api/v1/me").at(-1);
  assert.equal(last.headers.get("Authorization"), null);
  assert.deepEqual(await violations(), []);
});

test("config: theme passed to the components inside, host tokens honoured", async () => {
  const { config, app } = await mount(
    '<ameesh-config api-base="http://127.0.0.1:8780" theme="dark"><ameesh-app></ameesh-app></ameesh-config>');
  assert.equal(app.getAttribute("theme"), "dark");
  config.theme = "light";
  await settle(config);
  assert.equal(app.getAttribute("theme"), "dark"); // set once, the host's to change
});

test("config without api-base: said, accessibly", async () => {
  const { config } = await mount("<ameesh-config><ameesh-app></ameesh-app></ameesh-config>");
  assert.match(config.shadowRoot.textContent, /api-base/);
  assert.deepEqual(await violations(), []);
});
