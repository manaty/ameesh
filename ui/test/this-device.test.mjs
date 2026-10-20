// SPDX-License-Identifier: AGPL-3.0-only
// L93: <ameesh-this-device> renders the local state a host supplies and
// raises the stop event; it never calls a mesh API, never opens the socket.
import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { settle, ui, violations, window } from "./dom.mjs";

const { document } = window;

const STATE = {
  schema: "ameesh-device/1", generated_ts: 1792534585, host: "pc-test",
  runners: [{ id: "runner-a@pc-test", pid: 42, since_ts: 1792530000,
    socket: "/run/user/1000/ameesh/device-a.sock",
    control_socket: "/run/user/1000/ameesh/control-a.sock" }],
  meshes: [{ mesh: "manaty", organisation: "Manaty", agents: [{
    name: "agent-a", persona: "agent-a", harness: "deepseek", status: "running",
    status_text: "turn: build", project: "ameesh",
    lot: { id: 382, title: "L93: my workstation", state: "build", app: "ameesh" },
    turn: { active: true, started_ts: 1792533973, label: "messages" },
    lease: { live: true, owner: "runner-a@pc-test", expires_ts: 1792534773 },
    sandbox: { measured: true, cpu: 0.12, memory_bytes: 805306368,
      memory_max_bytes: 6442450944, memory_used: 0.125, network: null },
    runner: "runner-a@pc-test", since_ts: 1792533973,
  }] }],
  resources: { sampled_ts: 1792534580, cpu_count: 8, load1: 1.5,
    mem_available_bytes: 30064771072, mem_total_bytes: 33648889856, turns_in_progress: 1 },
  compute: { gate: { state: "available", seq: 8, reason: "idle" },
    lease: { id: "lease-7", expires_ts: 1792534773 } },
  unmeasured: ["network"],
};

function mount(state = STATE) {
  const el = document.createElement("ameesh-this-device");
  el.state = state;
  document.body.append(el);
  return el;
}

const text = (el) => el.shadowRoot.textContent.replace(/\s+/g, " ").trim();

beforeEach(() => {
  document.body.replaceChildren();
  document.documentElement.lang = "en";
});

test("device: the local state is rendered without any mesh request", async () => {
  const calls = [];
  window.fetch = async (url) => { calls.push(String(url)); throw new Error("no fetch"); };
  globalThis.fetch = window.fetch;
  const el = mount();
  await settle(el);
  assert.match(text(el), /My workstation/);
  assert.match(text(el), /Workstation pc-test/);
  assert.match(text(el), /Manaty/);
  assert.match(text(el), /agent-a/);
  assert.match(text(el), /#382 L93: my workstation/);
  assert.match(text(el), /0\.12 CPU/);
  assert.match(text(el), /Compute gate: available \(idle\)/);
  assert.match(text(el), /Compute lease: lease-7/);
  assert.match(text(el), /Not measured: network/);
  assert.deepEqual(calls, [], "the component must not call the mesh API");
  assert.deepEqual(await violations(), []);
});

test("device: a clean stop raises identifiers without a control socket", async () => {
  const events = [];
  document.addEventListener("ameesh:stop", (event) => events.push(event.detail));
  const el = mount();
  await settle(el);
  const button = [...el.shadowRoot.querySelectorAll("button")]
    .find((item) => item.textContent.trim() === "Stop cleanly");
  assert.ok(button, "the clean stop button exists");
  button.click();
  assert.deepEqual(events, [{
    agent: "agent-a", runner: "runner-a@pc-test", mesh: "manaty",
    mode: "clean",
  }]);
});

test("device: the immediate stop is a distinct, host-decided gesture", async () => {
  const events = [];
  document.addEventListener("ameesh:stop", (event) => events.push(event.detail));
  const el = mount();
  await settle(el);
  const button = [...el.shadowRoot.querySelectorAll("button")]
    .find((item) => item.textContent.trim() === "Stop immediately");
  assert.ok(button, "the immediate stop button exists");
  button.click();
  assert.deepEqual(events, [], "the initial click must not stop the sandbox");
  await settle(el);
  assert.match(text(el), /Stop agent-a immediately.*unfinished work may be lost/);
  assert.deepEqual(await violations(), []);
  const confirm = [...el.shadowRoot.querySelectorAll("button")]
    .find((item) => item.textContent.trim() === "Confirm immediate stop");
  confirm.click();
  assert.equal(events[0].mode, "immediate");
  assert.equal(events[0].agent, "agent-a");
  assert.equal("control_socket" in events[0], false);
});

test("device: a host provider feeds the screen, and a refused state is said", async () => {
  const el = document.createElement("ameesh-this-device");
  el.provider = async () => STATE;
  el.poll = 0;
  document.body.append(el);
  await settle(el);
  await el.refresh();
  await settle(el);
  assert.match(text(el), /agent-a/);

  el.provider = async () => { throw new Error("socket closed"); };
  await el.refresh();
  await settle(el);
  assert.match(text(el), /Local state unreadable: socket closed/);
});

test("device: connectHost routes the stop to the host's stopAgent", async () => {
  const calls = [];
  const unbind = ui.connectHost({
    getToken: () => null, openApproval: () => {}, notify: () => {},
    stopAgent: (agent, options) => calls.push([agent, options]),
  });
  const el = mount();
  await settle(el);
  const button = [...el.shadowRoot.querySelectorAll("button")]
    .find((item) => item.textContent.trim() === "Stop cleanly");
  button.click();
  assert.deepEqual(calls, [["agent-a", {
    mode: "clean", runner: "runner-a@pc-test", mesh: "manaty",
  }]]);
  unbind();
});

test("device: an unavailable state from the socket is said, not rendered as empty", async () => {
  const el = mount({ schema: "ameesh-device/1", error: "unavailable", message: "database down" });
  await settle(el);
  assert.match(text(el), /Local state unreadable: database down/);
});

test("device: without a host provider, the limit is stated instead of pretending", async () => {
  const el = document.createElement("ameesh-this-device");
  document.body.append(el);
  await settle(el);
  assert.match(text(el), /only by the host application/);
  assert.match(text(el), /a web page cannot open it/);
  assert.deepEqual(await violations(), []);
});


test("device: cancelling the immediate stop does not emit a request", async () => {
  const calls = [];
  const unbind = ui.connectHost({
    getToken: () => null, openApproval: () => {}, notify: () => {},
    stopAgent: (...args) => calls.push(args),
  });
  try {
    const el = mount();
    await settle(el);
    const click = (label) => [...el.shadowRoot.querySelectorAll("button")]
      .find((item) => item.textContent.trim() === label).click();
    click("Stop immediately");
    await settle(el);
    click("Cancel");
    await settle(el);
    assert.deepEqual(calls, []);
    assert.equal(el.shadowRoot.querySelector('[role="group"]'), null);
  } finally { unbind(); }
});

test("device: forged page socket paths never cross the host binding", () => {
  const calls = [];
  const unbind = ui.connectHost({
    getToken: () => null, openApproval: () => {}, notify: () => {},
    stopAgent: (...args) => calls.push(args),
  });
  try {
    document.dispatchEvent(new CustomEvent("ameesh:stop", { detail: {
      agent: "agent-a", mode: "clean", runner: "runner-a@pc-test", mesh: "manaty",
      control_socket: "/attacker/chosen.sock",
    } }));
    assert.deepEqual(calls, [["agent-a", {
      mode: "clean", runner: "runner-a@pc-test", mesh: "manaty",
    }]]);
  } finally { unbind(); }
});
