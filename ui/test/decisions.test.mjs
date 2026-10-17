// SPDX-License-Identifier: AGPL-3.0-only
// L90: <ameesh-decisions> — the human's queue and the approval path. The
// button files the request (L87) and emits `ameesh:approval-requested`; it
// never approves, and no component opens the link (contract §2.2): the host
// opens it, and the standalone page shows it for the human to click.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { fakeFetch, settle, ui, violations, window } from "./dom.mjs";
const { document } = window;
import { routes } from "./read-fixtures.mjs";
const text = (el) => el.shadowRoot.textContent.replace(/\s+/g, " ").trim();
const ACTION = "act_0123456789abcdef";
const LINK = "https://ameesh.example/a/tok_single_use";
beforeEach(() => { document.body.replaceChildren(); document.documentElement.lang = "en"; });

const POST_ROUTE = "/api/v1/actions/" + ACTION + "/approval-request";
const REPLY = [200, { schema: "ameesh-action/1", action: "approval_request",
  by: "human:ada", for: "human:ada",
  result: { request_id: "req-1", action_id: ACTION, approver: "human:ada", link: LINK,
    link_exp: 1792003600, digest: "sha256:abc" } }];

function fixture(overrides = {}) {
  const fetch = fakeFetch({ ...routes, [POST_ROUTE]: REPLY, ...overrides });
  const client = ui.createClient({ baseUrl: "http://127.0.0.1:8780", tokenProvider: () => "tok",
    fetch });
  return { client, fetch };
}
async function mount(client, props = {}, tag = "ameesh-decisions") {
  const el = document.createElement(tag); el.client = client; Object.assign(el, props);
  let main = document.querySelector("main");
  if (!main) { main = document.createElement("main"); document.body.append(main); }
  main.append(el); await settle(el); return el;
}

test("the decisions screen shows the queue: approvals, requests, waiting lots and prose", async () => {
  const { client } = fixture();
  const el = await mount(client);
  assert.match(text(el), /Actions to approve/);
  assert.match(text(el), new RegExp(ACTION));
  assert.match(text(el), /stripe · refund · money/);
  assert.match(text(el), /4,?200 EUR|4200 EUR/);
  assert.match(text(el), /Requests from agents/);
  assert.match(text(el), /Which <b>option<\/b>\?/);   // untrusted text stays literal
  assert.match(text(el), /Request #12/);
  assert.match(text(el), /Lots waiting for a human/);
  assert.match(text(el), /#9 Awaiting answer/);
  assert.match(text(el), /Decisions asked in prose/);
  assert.match(text(el), /agent:ada should file it/);
  assert.equal(el.shadowRoot.querySelector("b"), null);
  assert.deepEqual(await violations(), []);
});

test("Approve files the request once, keeps the key on a retry, never approves", async () => {
  const { client, fetch } = fixture();
  const keys = [];
  let failing = true;
  const wrapped = { ...client, post: async (path, options) => {
    keys.push(options.headers["Idempotency-Key"]);
    if (failing) throw new ui.ApiError(502, "approve_unreachable", "ameesh-approve unreachable");
    return client.post(path, options);
  } };
  const el = await mount(wrapped);
  const events = [];
  el.parentNode.addEventListener("ameesh:approval-requested", (event) => events.push(event.detail));
  assert.match(text(el), /never here/);
  el.shadowRoot.querySelector(".cards button").click(); await settle(el);
  assert.match(text(el), /not filed \(approve_unreachable\)/);
  assert.deepEqual(events, []);
  failing = false;
  el.shadowRoot.querySelector(".cards button").click(); await settle(el);
  const posts = fetch.calls.filter((call) => call.init?.method === "POST");
  assert.equal(posts.length, 1);          // the refused try never reached the mesh
  assert.equal(posts[0].path, POST_ROUTE);
  assert.equal(posts[0].headers.get("Content-Type"), "application/json");
  assert.equal(keys.length, 2);
  assert.equal(keys[0], keys[1]);         // one key per action: a retry replays
  assert.match(keys[0], /^[A-Za-z0-9_-]{8,128}$/);
  assert.deepEqual(events, [{ action: ACTION, url: LINK }]);
  assert.match(text(el), /Request filed:/);
  const link = el.shadowRoot.querySelector("a");
  assert.equal(link.getAttribute("href"), LINK);
  assert.equal(link.getAttribute("target"), "_blank");
});

test("a refused filing is reported under the action and allows another try", async () => {
  let refused = true;
  const { client, fetch } = fixture();
  const wrapped = { ...client, post: (path, options) => refused
    ? Promise.reject(new ui.ApiError(503, "approve_not_configured", "no ameesh-approve"))
    : client.post(path, options) };
  const el = await mount(wrapped);
  el.shadowRoot.querySelector(".cards button").click(); await settle(el);
  assert.match(text(el), /not filed \(approve_not_configured\)/);
  refused = false;
  el.shadowRoot.querySelector(".cards button").click(); await settle(el);
  assert.match(text(el), /Request filed:/);
  assert.equal(fetch.calls.filter((call) => call.init?.method === "POST").length, 1);
});

test("no component opens a link: the event bubbles to the host, the link stays for the human", async () => {
  const opened = [];
  const realOpen = window.open;
  window.open = (url, target) => { opened.push([url, target]); return null; };
  try {
    const { client } = fixture();
    const app = await mount(client, { standalone: true, section: "decisions" }, "ameesh-app");
    // the host listens above the components, as the contract (§2.2) requires
    const seen = [];
    document.body.addEventListener("ameesh:approval-requested", (event) => seen.push(event.detail));
    app.shadowRoot.querySelectorAll("nav button")[1].click(); await settle(app);
    const view = app.shadowRoot.querySelector("ameesh-decisions"); await settle(view);
    view.shadowRoot.querySelector(".cards button").click(); await settle(view); await settle(app);
    assert.deepEqual(seen, [{ action: ACTION, url: LINK }]);  // composed: crosses both shadow roots
    assert.deepEqual(opened, []);                             // nothing opened by the components
    const link = view.shadowRoot.querySelector("a");          // the human opens it, in the page
    assert.equal(link.getAttribute("href"), LINK);
    assert.equal(link.getAttribute("target"), "_blank");
    assert.equal(link.getAttribute("rel"), "noopener noreferrer");
  } finally {
    window.open = realOpen;
  }
});
