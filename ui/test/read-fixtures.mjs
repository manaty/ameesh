// SPDX-License-Identifier: AGPL-3.0-only
const stamp = 1792000000;
const lot = { id: 7, title: "Read screens", state: "qa", assignee: "ada",
  milestones: { requested: stamp - 300, frozen: stamp - 200, verdict: stamp - 100, merged: null } };
const agent = { name: "ada", state: "working", host: "laptop", harness: "codex",
  project: "ameesh", since_ts: stamp, payment: "token", usd_24h: 2, unread: 3, lot, last_update: { text: "Reviewing" } };
const project = { name: "ameesh", lots_open: 1, usd_24h_token: 2, usd_24h: 4,
  unread: 3, agents: [agent], lots_without_agent: [], warnings: [] };
const progress = { schema: "ameesh-progress/1", generated_ts: stamp,
  window: { from_ts: stamp - 400, to_ts: stamp }, lots: [lot], budget: {
    accounts: [{ harness: "codex", account: "primary", active: true, gauges: [{ key: "5h", used: 0.4, pace_cap: 0.6, resets_ts: stamp + 500 }] }],
    real: [{ provider: "deepseek", real_spent: 0.12, currency: "USD", period: "24h" }], plans: [],
  } };
const timeline = { schema: "ameesh-timeline/1", generated_ts: stamp,
  window: { from_ts: stamp - 400, to_ts: stamp }, lots: [lot] };
const routes = {
  "/.well-known/ameesh": [200, { organisation: "Acme", version: "1.11", mode: "local" }],
  "/api/v1/me": [200, { human: "human:ada", language: "en" }],
  "/api/v1/projects": [200, { projects: [project] }],
  "/api/v1/projects?project=ameesh": [200, { projects: [project] }],
  "/api/v1/projects/ameesh/progress": [200, progress],
  "/api/v1/projects/ameesh/timeline": [200, timeline],
  "/api/v1/agents/ada": [200, { agent: { name: "ada", key_ready: true } }],
  "/api/v1/alerts": [200, { alerts: [{ type: "stale", agent: "ada", responsible: "human:ada", detail: "Needs attention" }] }],
  "/api/v1/hosts": [200, { hosts: [{ host: "laptop", max_agents: 4, responsible: "human:ada",
    pressure: { blocked: true, critical: false, breaches: [{ key: "max_load", value: 8, limit: 4 }] },
    latest: { sampled_ts: stamp, load1: 8, mem_available_bytes: 8 * 2 ** 30, swap_used_bytes: 0 }, fiches: [] }] }],
  "/api/v1/budget": [200, { limits: { per_hour_usd: 10, per_day_usd: null }, spend: { "1h_counted_usd": 2, "24h_counted_usd": 5 }, paused: [] }],
  "/api/v1/threads/ameesh": [200, { entries: [] }],
  "/api/v1/threads/ameesh/7": [200, { entries: [{ author: "agent:ada", recipient: "human:ada", ts: stamp,
    text: "<img src=x onerror=alert(1)>\n**literal message**", id: "1" }] }],
  // L90: the human's queue, in the shape of `GET /api/v1/decisions`
  "/api/v1/decisions": [200, {
    for: "human:ada",
    approvals: [{ action_id: "act_0123456789abcdef", project: "ameesh", work_item: 7,
      proposed_by: "agent:ada", connector: "stripe", operation: "refund", target: "ch_1",
      class: "money", amount: 4200, currency: "EUR", digest: "sha256:abc", state: "proposed" }],
    requests: [{ id: 12, project: "ameesh", lot: 7, lot_title: "Read screens",
      requester: "agent:ada", owner: "human:ada", question: "Which <b>option</b>?",
      options: [{ key: "a", label: "ship" }, { key: "b", label: "wait" }], recommend: "a",
      urgent: false, overdue: false, created_ts: stamp, due_ts: stamp + 3600, state: "pending" }],
    waiting_human: [{ id: 9, title: "Awaiting answer", state: "waiting_human",
      assignee: "ada", project: "ameesh" }],
    unknown: [], interrupted: [],
    prose: [{ id: 31, sender: "agent:ada", recipient: "human:ada", lot: 7,
      created_ts: stamp - 60, age_s: 60, cues: ["il faudrait trancher"],
      excerpt: "il faudrait trancher : <img src=x>", convert: "ameesh decide ask --from-mail 31" }],
  }],
};

export { routes };
