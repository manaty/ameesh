// SPDX-License-Identifier: AGPL-3.0-only
// Fixtures of the L92 example: a fictional organisation, "Acme", and the read
// API of its mesh, as the components consume it (study §2.1). The same data
// feeds the fixtures server (fixtures-server.mjs) and the jsdom proof
// (ui/test/example.test.mjs): no clock, no random, the same bytes every run.
export const ORGANISATION = "Acme";
export const MESH_ID = "acme";
export const PROJECT = "acme-portal";
/** The token the example host hands to the components; the fixtures server
 *  accepts this one and refuses the others (401, like a mesh would). */
export const DEMO_TOKEN = "acme-demo-token";

const stamp = 1792500000;
const day = 86400;

const ada = {
  name: "ada", state: "working", host: "laptop-ada", harness: "codex",
  project: PROJECT, since_ts: stamp - 1800, payment: "token", usd_24h: 2.4, unread: 1,
  lot: { id: 12, title: "Panier d'achat", state: "qa", assignee: "ada",
    milestones: { requested: stamp - 3 * day, frozen: stamp - 2 * day,
      verdict: stamp - day, merged: null } },
  last_update: { text: "Relecture du panier : deux défauts corrigés." },
};
const grace = {
  name: "grace", state: "qa", host: "vm-grace", harness: "claude",
  project: PROJECT, since_ts: stamp - 7200, payment: "plan", usd_24h: 0.5, unread: 1,
  lot: { id: 18, title: "Facturation du portail", state: "build", assignee: "grace",
    milestones: { requested: stamp - 2 * day, frozen: stamp - day,
      verdict: null, merged: null } },
  last_update: { text: "TVA : le taux du client l'emporte sur le défaut." },
};

const discovery = {
  schema: "ameesh-discovery/1", organisation: ORGANISATION, api: "/api/v1",
  versions: ["v1"], version: "1.11.4", mode: "local", oidc_issuer: null,
  approve_url: "/a/demo", languages: ["fr", "en"], openapi: "/api/v1/openapi.json",
};
const me = {
  schema: "ameesh-me/1", human: "human:ada", language: "en", mode: "local",
  organisation: ORGANISATION, roles: ["project-lead"], deputyships: [],
};
const project = {
  name: PROJECT, lots_open: 2, usd_24h_token: 3.1, usd_24h: 4.9, unread: 2,
  agents: [ada, grace], lots_without_agent: [], warnings: [],
};
const progress = {
  schema: "ameesh-progress/1", generated_ts: stamp,
  window: { from_ts: stamp - day, to_ts: stamp },
  lots: [ada.lot, grace.lot],
  budget: {
    accounts: [{ harness: "codex", account: "primary", active: true,
      gauges: [{ key: "5h", used: 0.72, pace_cap: 0.8, resets_ts: stamp + 7200 }] }],
    real: [{ provider: "deepseek", real_spent: 1.12, currency: "USD", period: "24h" }],
    plans: [],
  },
};

const timeline = {
  schema: "ameesh-timeline/1", generated_ts: stamp,
  window: { from_ts: stamp - day, to_ts: stamp },
  lots: [ada.lot, grace.lot],
};

/** Read routes of the fictional mesh: path → [status, body]. */
export const routes = {
  "/.well-known/ameesh": [200, discovery],
  "/api/v1/me": [200, me],
  "/api/v1/projects": [200, { projects: [project] }],
  ["/api/v1/projects/" + PROJECT + "/progress"]: [200, progress],
  ["/api/v1/projects/" + PROJECT + "/timeline"]: [200, timeline],
  "/api/v1/agents/ada": [200, { agent: { ...ada, key_ready: true } }],
  "/api/v1/agents/grace": [200, { agent: { ...grace, key_ready: true } }],
  "/api/v1/alerts": [200, { alerts: [
    { type: "budget", agent: "ada", responsible: "human:ada",
      detail: "Codex 5h at 72% of its pace cap" },
    { type: "stale", agent: "grace", responsible: "human:ada",
      detail: "Lot #18 without movement for 2 h" },
  ] }],
  "/api/v1/hosts": [200, { hosts: [
    { host: "laptop-ada", max_agents: 4, responsible: "human:ada",
      pressure: { blocked: false, critical: false, breaches: [] },
      latest: { sampled_ts: stamp, load1: 0.8, mem_available_bytes: 12 * 2 ** 30,
        swap_used_bytes: 0 },
      fiches: [] },
  ] }],
  "/api/v1/budget": [200, { limits: { per_hour_usd: 5, per_day_usd: 40 },
    spend: { "1h_counted_usd": 1.2, "24h_counted_usd": 4.9 }, paused: [] }],
  ["/api/v1/threads/" + PROJECT]: [200, { entries: [
    { author: "agent:ada", recipient: "human:ada", ts: stamp - 600, id: "1",
      text: "Le panier passe en relecture.\n<b>ce texte reste littéral</b>" },
  ] }],
  ["/api/v1/threads/" + PROJECT + "/12"]: [200, { entries: [
    { author: "agent:ada", recipient: "human:ada", ts: stamp - 300, id: "2",
      text: "Deux défauts du panier corrigés, témoins verts." },
  ] }],
};

/** A route table for a fake fetch (path + search → [status, body]). */
export function routeMap() {
  return { ...routes, ["/api/v1/projects?project=" + PROJECT]: routes["/api/v1/projects"] };
}

/** The fixture of `pathname` (query ignored, like the fixture server). */
export function resolve(pathname) {
  return routes[pathname] ?? null;
}

/** The bearer token of a request, `null` when there is none. */
export function bearer(header) {
  const match = /^Bearer (.+)$/.exec(header ?? "");
  return match ? match[1].trim() : null;
}
