// SPDX-License-Identifier: AGPL-3.0-only
import assert from "node:assert/strict";
import { test } from "node:test";
import { fakeFetch, ui } from "./dom.mjs";

test("event stream: fields, multi-line data, ids carried over, comments, CRLF split", () => {
  const parser = new ui.EventStreamParser();
  assert.deepEqual(parser.push(": heartbeat\n"), []);
  assert.deepEqual(parser.push("id: 7\nevent: lot\ndata: a\ndata:  b\n\n"),
    [{ type: "lot", data: "a\n b", id: "7" }]);
  assert.deepEqual(parser.push("data: c\r"), []);
  assert.deepEqual(parser.push("\n\r\n"), [{ type: "message", data: "c", id: "7" }]);
  assert.deepEqual(parser.push("retry: 2500\nretry: x\n\n"), []);
  assert.equal(parser.retry, 2500);
  assert.deepEqual(parser.push("data: half"), []);
  assert.deepEqual(parser.push(" done\n\n"), [{ type: "message", data: "half done", id: "7" }]);
  // a NUL in an id is ignored (spec)
  parser.push("id: a\0b\ndata: d\n\n");
  assert.equal(parser.lastId, "7");
});

test("stream: reconnects with Last-Event-ID, stops on a refusal", async () => {
  const seen = [];
  const requests = [];
  let n = 0;
  const client = {
    request: async (path, init) => {
      requests.push(new Headers(init.headers).get("Last-Event-ID"));
      n += 1;
      if (n === 1) return new Response("id: 1\ndata: one\n\n");
      if (n === 2) return new Response("", { status: 503 });
      if (n === 3) return new Response("id: 2\ndata: two\n\n");
      return new Response("", { status: 401, statusText: "Unauthorized" });
    },
  };
  const states = [];
  let error = null;
  await ui.readStream(client, "/api/v1/events", {
    onEvent: (e) => seen.push(e.data), onState: (c) => states.push(c),
    onError: (e) => { error = e; }, delay: 1, maxDelay: 2,
  });
  assert.deepEqual(seen, ["one", "two"]);
  assert.deepEqual(requests, [null, "1", "1", "2"]);
  assert.equal(error.status, 401);
  assert.deepEqual(states, [true, false, false, true, false]);
});

test("stream: abort ends it", async () => {
  const stop = new AbortController();
  const client = { request: async () => { stop.abort(); throw new Error("aborted"); } };
  await ui.readStream(client, "/x", { onEvent() {}, signal: stop.signal });
});

test("client: token per request, typed path, query, errors", async () => {
  const fetch = fakeFetch({
    "/api/v1/lots/7": [200, { schema: "ameesh-lot/1", id: 7 }],
    "/api/v1/lots?state=build&limit=5": [200, { schema: "ameesh-lots/1", lots: [] }],
    "/api/v1/me": [401, { schema: "ameesh-error/1", error: "unauthorized", message: "refused" }],
  });
  let token = "secret";
  const refused = [];
  const client = ui.createClient({ baseUrl: "http://127.0.0.1:8780/", tokenProvider: () => token, fetch,
    onUnauthorized: (t) => refused.push(t) });
  assert.equal((await client.get("/api/v1/lots/{id}", { path: { id: 7 } })).id, 7);
  await client.get("/api/v1/lots", { query: { state: "build", limit: 5, assignee: undefined } });
  assert.equal(fetch.calls[0].headers.get("Authorization"), "Bearer secret");
  assert.equal(fetch.calls[0].init.credentials, "omit");
  token = null;
  await assert.rejects(client.get("/api/v1/me"),
    (e) => e instanceof ui.ApiError && e.status === 401 && e.code === "unauthorized");
  assert.equal(fetch.calls[2].headers.get("Authorization"), null);
  token = "stale";
  await assert.rejects(client.get("/api/v1/me"), ui.ApiError);
  assert.deepEqual(refused, [null, "stale"]); // each 401, with the token it refused
  assert.equal(ui.expand("/t/{project}/{lot}", { project: "a/b", lot: "1 2" }), "/t/a%2Fb/1%202");
  assert.throws(() => ui.expand("/t/{project}", {}), TypeError);
});

test("client: the token never leaves the mesh's origin", async () => {
  const fetch = fakeFetch({ "/mesh/api/v1/me": [200, { schema: "ameesh-me/1" }] });
  const client = ui.createClient({ baseUrl: "https://api.example/mesh/", tokenProvider: () => "secret", fetch });
  for (const path of [".evil.example/api/v1/me", "//evil.example/api/v1/me", "api/v1/me",
                      "@evil.example/x", "https://evil.example/x"]) {
    await assert.rejects(client.request(path), TypeError, path);
  }
  assert.equal(fetch.calls.length, 0);
  await client.request("/api/v1/me");
  assert.equal(fetch.calls[0].path, "/mesh/api/v1/me");
  const bare = ui.createClient({ baseUrl: "https://api.example", tokenProvider: () => "secret", fetch });
  await assert.rejects(bare.request("//evil.example/x"), TypeError);
  assert.equal(fetch.calls.length, 1);
});

test("stream: the server's retry stays within bounds, oversized ones ignored", async () => {
  const parser = new ui.EventStreamParser();
  parser.push("retry: 99999999999999999999\n\n");
  assert.equal(parser.retry, null);
  const delays = [];
  const realTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms) => { delays.push(ms); return realTimeout(fn, 0); };
  try {
    let n = 0;
    const client = { request: async () => {
      n += 1;
      if (n === 1) return new Response("retry: 9999999999\ndata: a\n\n");
      if (n === 2) return new Response("retry: 0\ndata: b\n\n");
      return new Response("", { status: 401 });
    } };
    await ui.readStream(client, "/x", { onEvent() {}, delay: 1000, maxDelay: 30000 });
  } finally {
    globalThis.setTimeout = realTimeout;
  }
  assert.deepEqual(delays, [30000, 500]);
});

test("i18n: both catalogues, plurals, parameters as text", () => {
  assert.equal(ui.translate("fr", "count.alerts", { n: 0 }), "0 alerte");
  assert.equal(ui.translate("en", "count.alerts", { n: 0 }), "0 alerts");
  assert.equal(ui.translate("en", "count.lots", { n: 1 }), "1 lot");
  assert.equal(ui.translate("fr", "count.lots", { n: 1200 }), "1 200 lots");
  assert.equal(ui.translate("de", "app.home"), "Overview");
  assert.equal(ui.translate("en", "app.connected", { organisation: "<b>x</b>" }), "Connected to <b>x</b>");
  assert.deepEqual([...ui.LANGUAGES].sort(), ["en", "fr"]);
});
