// SPDX-License-Identifier: AGPL-3.0-only
// L91: <ameesh-chat> tier 1 — the message goes to the agent's mailbox through
// the shared conversation (L83), the answer comes back through the thread,
// and the screen says the agent's state and when a message waits for its turn.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { fakeFetch, settle, ui, violations, window } from "./dom.mjs";
const { document } = window;
import { routes } from "./read-fixtures.mjs";
const text = (el) => el.shadowRoot.textContent.replace(/\s+/g, " ").trim();
beforeEach(() => { document.body.replaceChildren(); document.documentElement.lang = "en"; });

const CHAT = "/api/v1/agents/ada/chat";
const REPLY = [200, { schema: "ameesh-action/1", action: "chat", by: "human:ada", for: "human:ada",
  result: { agent: "ada", message_id: 41, lot: null, entry: { seq: 3, kind: "human_message" } } }];
const stamp = 1792000000;

function fixture(overrides = {}) {
  const fetch = fakeFetch({ ...routes, [CHAT]: REPLY, ...overrides });
  const client = ui.createClient({ baseUrl: "http://127.0.0.1:8780", tokenProvider: () => "tok", fetch });
  return { client, fetch };
}
async function mount(client, props = {}) {
  const el = document.createElement("ameesh-chat"); el.client = client; Object.assign(el, props);
  const main = document.createElement("main"); document.body.append(main);
  main.append(el); await settle(el); return el;
}

test("the conversation shows the exchanges with the agent from its project and lot threads", async () => {
  const { client, fetch } = fixture({
    "/api/v1/threads/ameesh": [200, { entries: [
      { id: "p1", author: "human:ada", recipient: "agent:ada", text: "Status?", ts: stamp - 10 },
      { id: "p2", author: "agent:bob", recipient: "human:ada", text: "not this agent", ts: stamp - 5 },
    ] }],
  });
  const el = await mount(client, { agent: "ada" });
  const body = text(el);
  // the lot thread's reply (current lot #7), after the project thread's question
  assert.ok(body.indexOf("Status?") < body.indexOf("**literal message**"));
  assert.doesNotMatch(body, /not this agent/);
  assert.equal(el.shadowRoot.querySelector("img"), null);   // untrusted text stays literal
  assert.match(body, /ada: Working/);
  assert.match(body, /3 unread/);
  // the agent answered last: nothing waits
  assert.doesNotMatch(body, /Waiting for the next turn/);
  assert.ok(fetch.calls.some((c) => c.path === "/api/v1/threads/ameesh/7"));
  assert.deepEqual(await violations(), []);
});

test("a message is sent once to the shared conversation, then waits for the next turn", async () => {
  const { client, fetch } = fixture();
  const keys = [];
  let failing = true;
  const wrapped = { ...client, post: async (path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    if (failing) throw new ui.ApiError(503, "unavailable", "down");
    return client.post(path, options);
  } };
  const el = await mount(wrapped, { agent: "ada", lot: "7" });
  el.draft = "Please rebase"; await settle(el);
  el.shadowRoot.querySelector("form button").click(); await settle(el);
  assert.match(text(el), /not sent \(unavailable\)/);
  assert.equal(el.draft, "Please rebase");
  failing = false;
  // the thread now ends with the human's message
  routes["/api/v1/threads/ameesh/7"][1].entries.push(
    { id: "2", author: "human:ada", recipient: "agent:ada", text: "Please rebase", ts: stamp + 5 });
  try {
    el.shadowRoot.querySelector("form button").click(); await settle(el);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);   // the retry replays, never sends twice
    const post = fetch.calls.find((c) => c.path === CHAT);
    assert.deepEqual(JSON.parse(post.init.body), { text: "Please rebase", lot: 7 });
    assert.equal(el.draft, "");
    assert.match(text(el), /Waiting for the next turn/);
    // a new message takes a new key
    el.draft = "And then?"; await settle(el);
    el.shadowRoot.querySelector("form button").click(); await settle(el);
    assert.notEqual(keys[2], keys[0]);
  } finally {
    routes["/api/v1/threads/ameesh/7"][1].entries.pop();
  }
});

test("a refused send takes a fresh key: the stored refusal does not block the retry", async () => {
  const { client } = fixture();
  const keys = [];
  let refused = true;
  const wrapped = { ...client, post: async (path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    if (refused) throw new ui.ApiError(409, "refused", "agent stopped");
    return client.post(path, options);
  } };
  const el = await mount(wrapped, { agent: "ada", lot: "7" });
  el.draft = "Hello"; await settle(el);
  el.shadowRoot.querySelector("form button").click(); await settle(el);
  assert.match(text(el), /not sent \(refused\)/);
  assert.equal(el.draft, "Hello");
  refused = false;
  el.shadowRoot.querySelector("form button").click(); await settle(el);
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1]);   // the stored refusal must not be replayed
  assert.equal(el.draft, "");
});

test("a paused agent: the message waits in its mailbox until resumed", async () => {
  const paused = structuredClone(routes["/api/v1/projects"]);
  paused[1].projects[0].agents[0].state = "paused";
  const { client } = fixture({
    "/api/v1/projects": paused,
    "/api/v1/threads/ameesh": [200, { entries: [
      { id: "q", author: "human:ada", recipient: "agent:ada", text: "Hello", ts: stamp + 100 }] }],
  });
  document.documentElement.lang = "fr";
  const el = await mount(client, { agent: "ada" });
  assert.match(text(el), /État de l'agent : En pause\. Votre message attend/);
});

test("a stopped agent: the send is refused at once, the screen asks to resume it", async () => {
  const stopped = structuredClone(routes["/api/v1/projects"]);
  stopped[1].projects[0].agents[0].state = "stopped";
  stopped[1].projects[0].agents[0].status = "stopped";
  const { client, fetch } = fixture({
    "/api/v1/projects": stopped,
    "/api/v1/threads/ameesh": [200, { entries: [
      { id: "q", author: "human:ada", recipient: "agent:ada", text: "Hello", ts: stamp + 100 }] }],
  });
  document.documentElement.lang = "fr";
  const el = await mount(client, { agent: "ada" });
  assert.match(text(el), /État de l'agent : Arrêté\./);
  assert.match(text(el), /reprenez d'abord l'agent avec `ameesh resume ada`/);
  assert.doesNotMatch(text(el), /attend dans sa boîte/);
  // the API refuses a stopped recipient (409 refused): the form is disabled
  assert.equal(el.shadowRoot.querySelector("textarea").disabled, true);
  assert.equal(el.shadowRoot.querySelector("form button").disabled, true);
  el.draft = "Hello"; await settle(el);
  await el.send();
  assert.ok(!fetch.calls.some((c) => c.path === CHAT));
});

test("a dead agent: the message waits in its mailbox until the executor takes it back", async () => {
  const dead = structuredClone(routes["/api/v1/projects"]);
  dead[1].projects[0].agents[0].state = "stopped";   // `progress` merges dead into stopped
  dead[1].projects[0].agents[0].status = "dead";
  const { client, fetch } = fixture({
    "/api/v1/projects": dead,
    "/api/v1/threads/ameesh": [200, { entries: [
      { id: "q", author: "human:ada", recipient: "agent:ada", text: "Hello", ts: stamp + 100 }] }],
  });
  document.documentElement.lang = "fr";
  const el = await mount(client, { agent: "ada" });
  assert.match(text(el), /État de l'agent : Hors ligne\./);
  assert.match(text(el), /Le bail de l'agent a expiré/);
  assert.doesNotMatch(text(el), /reprenez d'abord/);
  // a dead lease is still queued, not a manual stop: the form stays usable
  assert.equal(el.shadowRoot.querySelector("textarea").disabled, false);
  el.draft = "Hello"; await settle(el);
  el.shadowRoot.querySelector("form button").click(); await settle(el);
  assert.ok(fetch.calls.some((c) => c.path === CHAT));
});

test("without an agent, the screen asks for one and sends nothing", async () => {
  const { client, fetch } = fixture();
  const el = await mount(client);
  assert.match(text(el), /Choose an agent/);
  await el.send();
  assert.ok(!fetch.calls.some((c) => c.path === CHAT));
});

test("the server's recipient-free final answer appears and clears the waiting state", async () => {
  const { client } = fixture({
    "/api/v1/threads/ameesh/7": [200, { entries: [
      { id: "q", author: "human:ada", recipient: "agent:ada", text: "Status?", ts: stamp },
      // Shape produced by conversation.Log.drain_threads for turn_end.
      { id: "a", author: "agent:ada", recipient: "", text: "Finished", ts: stamp + 1,
        meta: { conversation: { agent: "ada", seq: 2, turn: "t1" } } },
      { id: "b", author: "agent:bob", recipient: "", text: "Another conversation", ts: stamp + 2,
        meta: { conversation: { agent: "bob", seq: 3, turn: "t2" } } },
      { id: "c", author: "agent:ada", recipient: "", text: "Unrelated broadcast", ts: stamp + 3 },
    ] }],
  });
  const el = await mount(client, { agent: "ada", lot: "7" });
  assert.match(text(el), /Finished/);
  assert.doesNotMatch(text(el), /Waiting for the next turn|Another conversation|Unrelated broadcast/);
});

test("after a failed send, changing its lot or agent takes a fresh idempotency key", async () => {
  const { client } = fixture();
  const keys = [];
  const wrapped = { ...client, post: async (_path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    throw new ui.ApiError(503, "unavailable", "down");
  } };
  const el = await mount(wrapped, { agent: "ada", lot: "7" });
  el.draft = "Status?";
  await el.send();
  await el.send();
  assert.equal(keys[0], keys[1]);
  el.lot = "8";
  await el.send();
  assert.notEqual(keys[1], keys[2]);
  el.agent = "bob";
  await el.send();
  assert.notEqual(keys[2], keys[3]);
});
