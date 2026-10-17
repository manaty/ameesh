// SPDX-License-Identifier: AGPL-3.0-only
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { fakeFetch, settle, ui, violations, window } from "./dom.mjs";
const { document } = window;
import { routes } from "./read-fixtures.mjs";
const text = (el) => el.shadowRoot.textContent.replace(/\s+/g, " ").trim();
beforeEach(() => { document.body.replaceChildren(); document.documentElement.lang = "en"; });

function fixture(overrides = {}, { live = false } = {}) {
  const fetch = fakeFetch({ ...routes, ...overrides });
  const streams = [];
  const client = ui.createClient({ baseUrl: "http://127.0.0.1:8780", tokenProvider: () => "tok",
    fetch: async (url, init) => {
      if (new URL(url).pathname !== "/api/v1/events" || !live) return fetch(url, init);
      const stream = { signal: init.signal };
      streams.push(stream);
      return new Response(new ReadableStream({ start(controller) {
        stream.send = (type) => controller.enqueue(new TextEncoder().encode(`event: ${type}\ndata: {}\n\n`));
        init.signal.addEventListener("abort", () => controller.close(), { once: true });
      } }), { headers: { "Content-Type": "text/event-stream" } });
    } });
  return { client, fetch, streams };
}
async function mount(tag, client, props = {}) {
  const el = document.createElement(tag); el.client = client; Object.assign(el, props);
  let main = document.querySelector("main");
  if (!main) { main = document.createElement("main"); document.body.append(main); }
  main.append(el); await settle(el); return el;
}

test("seven public read components render API data accessibly and keep untrusted text literal", async () => {
  const { client } = fixture();
  const props = { project: "ameesh", agent: "ada", lot: "7" };
  const cases = [["projects", /ameesh.*ada/], ["timeline", /Read screens.*Review/],
    ["agent", /ada.*laptop.*codex/], ["alerts", /Needs attention/], ["hosts", /laptop.*Warning.*8 GiB/],
    ["budget", /No cap.*primary.*40%.*60%/], ["thread", /<img src=x.*literal message/]];
  for (const [tag, expected] of cases) {
    const el = await mount("ameesh-" + tag, client, props);
    assert.match(text(el), expected, tag);
    assert.equal(el.shadowRoot.querySelector("img"), null);
    assert.match(text(el), /Live updates unavailable/);
  }
  assert.deepEqual(await violations(), []);
});

test("project, agent and lot links emit host events without changing the host URL", async () => {
  const { client } = fixture(); const el = await mount("ameesh-projects", client);
  const events = []; const url = window.location.href;
  const record = (e) => events.push([e.type, e.detail]);
  for (const kind of ["project", "agent", "lot"]) el.parentNode.addEventListener("ameesh:open-" + kind, record);
  for (const button of el.shadowRoot.querySelectorAll(".link")) button.click();
  assert.deepEqual(events, [["ameesh:open-project", { project: "ameesh" }],
    ["ameesh:open-agent", { agent: "ada", project: "ameesh" }], ["ameesh:open-lot", { project: "ameesh", lot: 7 }]]);
  assert.equal(window.location.href, url);
});

test("mounted views share a live stream, refresh after events and stop it on removal", async () => {
  const { client, fetch, streams } = fixture({}, { live: true });
  const a = await mount("ameesh-projects", client);
  const b = await mount("ameesh-alerts", client);
  assert.equal(streams.length, 1);
  assert.match(text(a), /Live/);
  const before = fetch.calls.length;
  routes["/api/v1/projects"][1].projects[0].unread = 8;
  streams[0].send("lot.updated"); streams[0].send("thread.entry"); streams[0].send("reset");
  await new Promise((resolve) => setTimeout(resolve, 1100)); await settle(a); await settle(b);
  assert.ok(fetch.calls.length > before);
  assert.match(text(a), /Unread\s*8/);
  a.remove(); assert.equal(streams[0].signal.aborted, false);
  b.remove(); await Promise.resolve(); assert.equal(streams[0].signal.aborted, true);
  routes["/api/v1/projects"][1].projects[0].unread = 3;
});

test("replacing the last mounted view keeps its stream until navigation finishes", async () => {
  const { client, streams } = fixture({}, { live: true });
  const first = await mount("ameesh-projects", client);
  first.remove();
  const next = document.createElement("ameesh-alerts");
  next.client = client;
  document.querySelector("main").append(next);
  await settle(next);
  assert.equal(streams.length, 1);
  assert.equal(streams[0].signal.aborted, false);
  streams[0].send("alert.raised");
  await settle(next);
  assert.match(text(next), /Live/);
  next.remove();
  await Promise.resolve();
  assert.equal(streams[0].signal.aborted, true);
});

test("switching a selection aborts the old request and cannot display its late data", async () => {
  let resolveOld, oldSignal;
  const { client } = fixture();
  const wrapped = { ...client, get: async (path, opts) => {
    if (path === "/api/v1/threads/{project}" && opts.path.project === "old") {
      oldSignal = opts.signal; return new Promise((resolve) => { resolveOld = resolve; });
    }
    return client.get(path, opts);
  } };
  const el = await mount("ameesh-thread", wrapped, { project: "old" });
  el.project = "ameesh"; await settle(el);
  assert.equal(oldSignal.aborted, true);
  resolveOld({ entries: [{ text: "PRIVATE OLD THREAD" }] }); await settle(el);
  assert.doesNotMatch(text(el), /PRIVATE OLD/);
  assert.match(text(el), /Nothing to show/);
});

test("a refused refresh clears a previous snapshot, reports the refusal and permits retry", async () => {
  const { client } = fixture(); let refused = false;
  const wrapped = { ...client, get: (path, opts) => {
    if (refused) return Promise.reject(new ui.ApiError(403, "forbidden", "no"));
    return client.get(path, opts);
  } };
  const el = await mount("ameesh-projects", wrapped);
  assert.match(text(el), /ada/);
  refused = true; await el.refresh(); await settle(el);
  assert.doesNotMatch(text(el), /ada/);
  assert.match(text(el), /Access refused/);
  refused = false; el.shadowRoot.querySelector("button").click(); await settle(el);
  assert.match(text(el), /ada/);
});

test("an event during a request schedules a fresh snapshot after that request", async () => {
  const { client, streams } = fixture({}, { live: true }); let release, calls = 0;
  const wrapped = { ...client, get: async () => {
    calls++;
    if (calls === 1) return new Promise((resolve) => { release = resolve; });
    return { projects: [{ name: "fresh", agents: [] }] };
  } };
  const el = await mount("ameesh-projects", wrapped);
  streams[0].send("lot.updated"); await settle(el);
  release({ projects: [{ name: "stale", agents: [] }] });
  await new Promise((resolve) => setTimeout(resolve, 1100)); await settle(el);
  assert.match(text(el), /fresh/);
  assert.doesNotMatch(text(el), /stale/);
  assert.equal(calls, 2);
});

test("the app assembles all seven screens and navigates project → lot thread internally", async () => {
  const { client } = fixture(); const app = await mount("ameesh-app", client);
  assert.equal(app.shadowRoot.querySelectorAll("nav button").length, 7);
  let view = app.shadowRoot.querySelector("ameesh-projects"); await settle(view);
  view.shadowRoot.querySelector(".link").click(); await settle(app);
  view = app.shadowRoot.querySelector("ameesh-timeline"); await settle(view);
  assert.equal(view.project, "ameesh");
  view.shadowRoot.querySelector(".link").click(); await settle(app);
  view = app.shadowRoot.querySelector("ameesh-thread"); await settle(view);
  assert.equal(view.lot, "7"); assert.match(text(view), /literal message/);
});

test("missing selections do not issue unscoped requests; unsupported live API keeps manual refresh", async () => {
  const { client, fetch } = fixture();
  const timeline = await mount("ameesh-timeline", client);
  const agent = await mount("ameesh-agent", client);
  assert.match(text(timeline), /Choose a project/); assert.match(text(agent), /Choose an agent/);
  assert.equal(fetch.calls.length, 0);
  timeline.project = "ameesh"; await settle(timeline);
  assert.match(text(timeline), /Read screens/);
});

test("configuration changes clear direct embedded views even when the client object is unchanged", async () => {
  const config = document.createElement("ameesh-config"); config.apiBase = "http://127.0.0.1:8780";
  config.tokenProvider = () => "first";
  const { client } = fixture();
  // Explicit client is stable across config notifications, as with a host that
  // rotates credentials in its tokenProvider closure.
  const el = document.createElement("ameesh-projects"); el.client = client; config.append(el);
  document.body.append(config); await settle(el);
  assert.match(text(el), /ada/);
  let release;
  client.get = () => new Promise((resolve) => { release = resolve; });
  config.disconnect(); await settle(el);
  assert.doesNotMatch(text(el), /ada/);
  release({ projects: [] }); await settle(el);
  assert.match(text(el), /Nothing to show/);
});

test("a stream token refusal clears an earlier snapshot and notifies the embedding host", async () => {
  const { client } = fixture(); let release;
  const wrapped = { ...client, request: () => new Promise((resolve) => { release = resolve; }) };
  const el = await mount("ameesh-projects", wrapped);
  assert.match(text(el), /ada/);
  let refusals = 0; el.addEventListener("ameesh:unauthorized", () => refusals++);
  release(new Response("", { status: 401 })); await settle(el);
  assert.doesNotMatch(text(el), /ada/);
  assert.match(text(el), /Token missing or refused/);
  assert.equal(refusals, 1);
});
