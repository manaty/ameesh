---
type: Study
title: "Study — an ameesh interface for humans, embeddable in Nexlink and in a third-party desktop application"
description: "Human actors and screens; versioned public API (read, actions, real time over SSE fed by LISTEN/NOTIFY, OpenAPI); web components with no imposed framework; Electron integration and Nexlink page; OIDC identity and roles, approval always by passkey; one service per mesh; link with Nexlink Compute and a single desktop application; lots L84–L93 and proposed decision."
status: draft
tags: [interface, api, sse, web-components, nexlink, electron, oidc, compute, accessibilite, i18n]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T20:00:00+02:00" }
sources:
  - { resource: "decisions/0003-ameesh-et-nexlink.md", title: "Decision 0003 (ameesh and Nexlink)" }
  - { resource: "decisions/0006-lisibilite-humaine.md", title: "Decision 0006 (readability)" }
  - { resource: "decisions/0012-autorite-par-ameesh-approve.md", title: "Decision 0012 (authority through ameesh-approve)" }
  - { resource: "decisions/0015-interfaces-des-facades.md", title: "Decision 0015 (facade interfaces)" }
  - { resource: "decisions/0017-approbation-via-page-nexlink.md", title: "Decision 0017 (Nexlink page)" }
  - { resource: "decisions/0024-vue-temps-reel.md", title: "Decision 0024 (real-time view)" }
  - { resource: "decisions/0026-hebergement-d-ameesh-approve-par-equipe.md", title: "Decision 0026 (one RP ID per team)" }
  - { resource: "decisions/0029-persona-et-session.md", title: "Decision 0029 (persona, visibility)" }
  - { resource: "decisions/0030-pas-de-travail-sans-reveil-possible.md", title: "Decision 0030 (pushed alerts, identity)" }
  - { resource: "decisions/0013-licence-agpl.md", title: "Decision 0013 (AGPL-3.0-only)" }
  - { resource: "../../src/ameesh/progress_page.html", title: "Reference view of ameesh progress (L24)" }
  - { resource: "../../src/ameesh/approve/server.py", title: "ameesh-approve server" }
  - { resource: "../../src/ameesh/fil.py", title: "Readable thread" }
  - { resource: "../../src/ameesh/notify.py", title: "Pushed alerts (L38)" }
  - { resource: "../../src/ameesh/actions_cli.py", title: "ameesh action, ameesh decisions" }
---

# Need

Request from the owner, 2026-10-10: "we are going to need a UI for ameesh,
easy to embed in Nexlink or in a future desktop app [of another
organisation], for the human actors". The request is tied to Nexlink Compute:
a workstation lent to a mesh must show what runs on it.

Today, a human follows ameesh through the command line (`ameesh list`,
`show`, `projects`, `progress`, `decisions`, `alerts`, `hosts`, `fil`), a
static page (`ameesh progress --html`), pushed alerts (`ameesh notify`) and
the ameesh-approve approval page. There is no network read API, no real-time
stream and no reusable component.

Constraints that frame the study:

* **ameesh stays autonomous** ([0003](../decisions/0003-ameesh-et-nexlink.md)):
  Nexlink is a native, priority connector, never a dependency of the core. A
  third-party application built on the public interfaces has the same
  possibilities as Nexlink ([0015](../decisions/0015-interfaces-des-facades.md)).
* **Authority stays a human passkey**
  ([0012](../decisions/0012-autorite-par-ameesh-approve.md)): no interface, no
  delegated token, no digital double approves.
* **Meshes are sealed per organisation** (decision 0033, branch
  `conception/v2-persona-roles`): a mesh has a single organisation, with no
  gateway between meshes; identity comes from an OIDC provider chosen by the
  organisation; human roles live in the canon; visibility for the person in
  charge, their deputies and their superiors (§9).
* **Everything stays readable** ([0006](../decisions/0006-lisibilite-humaine.md)):
  a conversation with an agent is an exchange like any other, and the thread
  keeps a record of it.
* **Zero runtime dependencies** on the Python side (`dependencies = []` in
  `pyproject.toml`); the `psycopg` and `crypto` extras are optional.

# What exists

| Piece | What it does | Reusable for the UI |
|---|---|---|
| `ameesh progress --json` / `--html` (L24, `progress.py`, `progress_page.html`) | `ameesh-progress/1` snapshot: lots on a timeline, agents, milestones, budget; standalone page, strict CSP, light and dark theme through CSS variables | **the data model and the mock-up** of the project view; the palette and the theme tokens |
| `ameesh projects --json` (L62, branch `lot/l62-vue-projets`) | `ameesh-projects/1`: per project, agents with state and reason, current lot, unread, 24 h spend, plan or token, lots without an agent, reports; one aggregated query (`projects.board`) | **the home screen** of a project lead, as is |
| `ameesh decisions --for human:ID` (C10, `actions_cli.py`) | queue of decisions waiting for a human (R17) | the decision queue |
| `ameesh action request` + ameesh-approve | files a request (`POST /requests`, service token), returns a `/a/<token>` link; the WebAuthn page signs; the receipt comes back through `GET /receipts/<id>` | **unchanged**: the UI opens this link, it never signs |
| ameesh-approve, `approve/server.py` | hardened stdlib `ThreadingHTTPServer`: CSP with no inline script, `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `Host` and `Origin` checks, bounded bodies, rate limiting, log with no secret | **the pattern** for the new API service (same stack, same defences); the approval page stays non-embeddable |
| readable thread, `fil.py` | one Markdown thread per lot or per project, append-only; `fil show`, `fil tail`; `thread_index` index | reading and publishing in a thread |
| `ameesh alerts --json`, `ameesh notify` (L38) | operations alerts; delivery to the person in charge (desktop, ntfy, Slack), deduplication, resolution, rate per human | the alerts panel; one more "application" channel |
| `ameesh hosts --json` (L31, 0028) | host resources, back-pressure | the hosts screen |
| `ameesh budget` (L70, in progress) and 0034 | mesh caps in the database; plan accounts as a reservoir | the budget and plans screen |
| LISTEN/NOTIFY (`db.py`, channels `agent_mail`, `agent_lease`) | wakes the runners; works with psycopg and with psql | **the source of real time** |

What is missing: an authenticated network API, a queryable event log (the
current channels only carry an identifier and cover neither lots, nor costs,
nor alerts), a network-side human identity, and interface components.

# 1. Human actors and needs

The actors below are **situations**, not exclusive roles: the same human is
often the lead of a project, the owner of a host and the provider of their own
workstation. They map onto the role base of 0033 §8 (coordinator, author,
reviewer, approver, referent, watcher), and deputyship (§7) gives the deputy
the same screens as the person in charge.

| Actor | What they want to know | What they want to do |
|---|---|---|
| **Project lead** (coordinator, often approver) | which lots progress, which are blocked, who works on what, what it costs, what waits for their decision | talk to the orchestrator or to an agent, reassign, pause, approve, set a cap |
| **Host owner** | what runs on their machines, their load, orphan sessions, runner alerts | drain a host, stop an agent, restart the runner, admit or remove a persona (PR to the canon) |
| **Compute provider** (lends a workstation) | what runs **right now** on their workstation, for whom, since when, with which resources | **stop** at once, suspend their offer, set their conditions (hours, sector, Wi-Fi) |
| **Requester** (human who orders work from the agents, or requester in the Compute sense) | where their request stands, the answer, the deliverable | formulate the request, answer the agent's questions, accept or refuse the deliverable |
| **Team member** (author, reviewer, referent, watcher) | the threads where they take part, the decisions that fall to them, the alerts that concern them | read a thread, write in it, review, approve if they are an approver |

## Screens and uses

Each screen matches a **component** (section 2.2) and API routes
(section 2.1).

1. **Conversation with an agent** — `<ameesh-chat>`. This is the central
   piece: a human talks to an agent (orchestrator, lot agent, their digital
   double) and sees the answer arrive. Two tiers:
   * **tier 1, no resident agent**: the message goes into the agent's mailbox
     (like `ameesh send`), the runner wakes the agent at the next turn, the
     answer arrives through the thread; the screen shows "waiting for the
     next turn" and the agent's state (working, idle, paused, stopped);
   * **tier 2, with the resident agent** (parallel study, L75–L83): the answer
     arrives continuously; the screen shows the agent's steps (tool used,
     file read) in summary, the cost of the turn, and allows interrupting.
   Both tiers write into the thread ([0006](../decisions/0006-lisibilite-humaine.md)).
2. **Decisions to take** — `<ameesh-decisions>`. The single queue of the
   human: gated actions waiting, lots under review that await their verdict,
   forced assignments, agent questions marked "waits for a human". Each entry
   shows the summary, the requester, the deadline and the amount. The
   **Approve** button does not approve: it opens the team's ameesh-approve
   page (section 2.4).
3. **Projects, agents and lots** — `<ameesh-projects>` and `<ameesh-timeline>`.
   The L62 view as the home, the timeline of 0024 as a project's detail, the
   card of an agent (state, reason, lot, current turn, unread, spend) and of a
   lot (milestones, blockers, thread).
4. **Alerts** — `<ameesh-alerts>`. Alerts raised and resolved, filtered by
   responsibility; read acknowledgement; link to the agent or the lot. In a
   desktop application, a system notification even with the window closed.
5. **Budget and plans** — `<ameesh-budget>`. Actual token spend, estimated
   plan spend, gauges for the windows (5 h, 7 days) and the allowed pace
   (0019), account reservoir and consumption order (0034), mesh caps (L70) and
   the pauses they cause.
6. **Hosts and resources** — `<ameesh-hosts>`. Hosts, live runners, memory and
   load (0028), sessions per host, admissions; for a lent workstation, the "my
   workstation" view (section 4).
7. **Readable thread** — `<ameesh-thread>`. Reading a lot or project thread,
   following it live, publishing a message signed with the human's identity.

All screens must be readable on a phone (requirement of 0024 and of 0033 §9).

# 2. Integration architecture

```
  host application (Nexlink desktop, Nexlink page, third-party app, standalone page)
  ┌───────────────────────────────────────────────────────────────┐
  │ <ameesh-app> / <ameesh-chat> / <ameesh-decisions> …            │
  │   ameesh client (fetch + SSE stream)  ← tokenProvider(mesh)    │
  └───────────────┬───────────────────────────────┬───────────────┘
                  │ HTTPS, Bearer (org's OIDC)    │ link opening (system browser
                  ▼                               ▼  or Nexlink facade)
        ameesh serve (API v1) ── LISTEN ──┐   ameesh-approve (host H, RP ID H)
                  │                      │         │
                  └──────── mesh PostgreSQL ───────┘
```

## 2.1 ameesh public API

A new service, **`ameesh serve`**, separate from ameesh-approve: another
process, another Unix user, another database role, **another origin**. A flaw
in the interface must never give access to the origin that holds the passkeys
(RP ID `H`, [0026](../decisions/0026-hebergement-d-ameesh-approve-par-equipe.md)).

**Stack.** The stdlib server of ameesh-approve (`ThreadingHTTPServer`) and its
defences, factored into a shared module: no new runtime dependency. One thread
per SSE connection is enough at the scale of an organisation (a few dozen
connected humans); the number of connections is bounded. If thousands of
streams are ever needed, we will move to an ASGI server as an optional extra,
without changing the contract.

**Versioning.** Prefix `/api/v1`. Each response carries its `schema`
(`ameesh-projects/1`, `ameesh-progress/1`…), like the current `--json`
outputs, which become the **same** serialisation: one single source of truth
between the CLI and the API. A field can be added without changing the
version; a removal or a change of meaning opens `/api/v2`, and `v1` is kept for
at least six months afterwards.

**Documentation.** An **OpenAPI 3.1** document served by
`GET /api/v1/openapi.json` and committed to the repository
(`docs/api/openapi-v1.json`). It is generated from the schemas of the JSON
outputs, and a test checks that each route answers in line with its schema.
The OpenAPI document also serves to generate the TypeScript client of the
components, and the client of a third-party application.

**Read** (all filtered by authorisations, section 2.5):

| Route | Content | Existing source |
|---|---|---|
| `GET /api/v1/me` | identity (`human:<id>`), organisation, roles, deputyships, language | canon (0033 §6) |
| `GET /api/v1/projects` | per-project view | `ameesh projects --json` (L62) |
| `GET /api/v1/projects/{p}/progress` | timeline, milestones, project budget | `ameesh progress --json` (L24) |
| `GET /api/v1/agents`, `/agents/{a}` | state, reason, lot, turn, unread, spend, host | `ameesh list` / `show` |
| `GET /api/v1/lots/{id}` | lot, milestones, blockers, assignee, thread | `work show`, `progress` |
| `GET /api/v1/decisions` | queue of the current human (R17) | `ameesh decisions --for` |
| `GET /api/v1/alerts` | recent raised and resolved alerts | `ameesh alerts --json` |
| `GET /api/v1/hosts`, `/hosts/{h}` | resources, runners, sessions | `ameesh hosts --json` |
| `GET /api/v1/budget` | caps, spend, gauges, reservoir | `ameesh budget --json` (L70), `cost.py` |
| `GET /api/v1/threads/{project}[/{lot}]?since=` | entries of a thread | `fil show` |

**Actions** (POST, all logged under the human's identity; a deputy is traced
"by X for Y", 0033 §7; `Idempotency-Key` header required):

| Route | Effect | Who (proposed default) |
|---|---|---|
| `POST /agents/{a}/messages` | message to an agent (mailbox + thread) | anyone who sees the agent |
| `POST /threads/{project}[/{lot}]` | publish in a thread | project members |
| `POST /agents/{a}/pause`, `/resume`, `/interrupt` | pause, resume, interrupt the turn | person in charge of the agent or project, deputies |
| `POST /agents/{a}/stop` | stop with a structured reason (0030 §4) | same, and the host owner |
| `POST /lots/{id}/assign` | reassign (rule of 0030 §2) | coordinator |
| `POST /alerts/{key}/ack` | acknowledge receipt | recipient |
| `POST /hosts/{h}/drain` | stop placing sessions on the host | host owner |
| `POST /budget/caps` | **lower** a cap | coordinator |
| `POST /actions/{id}/approval-request` | files the request with ameesh-approve, returns the **link** | designated approver |

What **commits** the organisation (executing a gated action, raising a cap,
forcing an assignment to an external agent, admitting a host) **never** goes
through an API route with the OIDC token alone: either it is a gated action
with a passkey receipt, or a PR to the canon. The API **has no approval
route**, and a test checks it (no route can produce or attach a receipt).

**Real time.** `GET /api/v1/events` as **Server-Sent Events**.

* **Source**: an append-only `ameesh_events` table (increasing identifier,
  type, object, project, timestamp), written in the same transaction as the
  change it describes (lot transition, turn start and end, lease, cost,
  alert, thread entry, decision created or closed, receipt attached). A
  trigger emits `pg_notify('ameesh_events', id)`, with the identifier alone
  (NOTIFY's 8000-byte limit, and no sensitive data in the channel).
* **Distribution**: a single `LISTEN` connection per `ameesh serve` process.
  For each subscriber, the server rereads the event, applies **that
  subscriber's** authorisations, then emits `event: lot.updated`, `id: <n>`
  and a short `data` (type, identifiers, summary). The client rereads the
  object through the API if it needs it. Resume after a cut through
  `Last-Event-ID` (replay from the table), heartbeat every 20 s, retention of
  the table per 0033 §9 (one year for the activity log).
* **Why SSE and not WebSocket**: the flow is downstream (actions are POSTs),
  SSE fits in the stdlib server, crosses proxies and relays with no tuning,
  and can resume. WebSocket would need a dependency or a protocol to write,
  for no gain here. The client reads the stream through `fetch` (not
  `EventSource`), to carry the `Authorization` header.
* **Conversation**: tier 2 broadcasts the answer fragments of a resident agent
  through the same mechanism (`event: chat.delta`), on a stream of its own for
  the conversation (`GET /api/v1/agents/{a}/chat/stream`), so as not to flood
  the general stream.

## 2.2 Embeddable interface components

| Option | For | Against |
|---|---|---|
| **Web components** (custom elements, Shadow DOM) | browser standard, no framework imposed on the host application (React, Vue, Svelte or nothing); isolated styles; theme through CSS variables and `::part`; work in Electron, in a web page and in a standalone page; one single package serves every case | needs some discipline (accessibility across the Shadow DOM, forms); older React host applications pass object properties less naturally |
| **iframe** of a page served by the mesh | maximum isolation; server-side updates without republishing the application | poor integration (height, navigation, theme, notifications, focus, screen reader); token to pass through `postMessage`; third-party cookies blocked in a web page; double loading |
| **npm package of framework components** (React…) | comfortable for a host that has this framework | imposes the framework and its version; unusable as is elsewhere; debt at the first major change |
| **Standalone page** served by the mesh | nothing to integrate; useful on its own (phone, workstation without Nexlink); installable (PWA) | it is not an integration; it does not live in the human's application |

**Recommendation: web components**, published in **two forms of the same
code**: an npm package (`@ameesh/ui`, ES module) for applications that embed
it, and a **file served by `ameesh serve`** (`/ui/ameesh-ui.js`), which also
serves the **standalone page** (`/ui/`, a full-screen `<ameesh-app>`). The
standalone page is therefore not a separate product: it is the default
assembly of the same components, for whoever has neither Nexlink nor an
application of their own ([0015](../decisions/0015-interfaces-des-facades.md):
do not impose Nexlink). The iframe is kept for nothing; the approval page, for
its part, always opens in the foreground (section 2.4).

Component contract:

* each component receives a **client** (`el.client = createClient({ baseUrl,
  tokenProvider })`) or, failing that, the `api-base` attributes and a
  `tokenProvider` set once by `<ameesh-config>`; the component never stores a
  token;
* inputs through simple attributes (`project`, `agent`, `lot`, `mesh`);
* outputs through prefixed **DOM events** (`ameesh:open-lot`,
  `ameesh:approval-requested` with the URL, `ameesh:alert` for a system
  notification): the host application decides how to navigate, open a link or
  notify;
* no access to `window.open`, cookies, local storage or the host page's
  navigation: everything goes through these events;
* a wrapper component, `<ameesh-app>`, assembles the screens with internal
  navigation, for one-line integration.

## 2.3 Integration

**Electron desktop application** (Nexlink, or that of another organisation):

* the components are loaded **in the application's renderer**, from the
  embedded npm package (no remote code executed); window with
  `contextIsolation: true`, `sandbox: true`, `nodeIntegration: false`;
* a **preload** exposes a minimal object through `contextBridge`, the host
  contract: `ameeshHost.getToken(mesh)` (the main process holds the OIDC
  tokens, sealed by `safeStorage`), `ameeshHost.openApproval(url)` (system
  browser, or the application's native facade), `ameeshHost.notify(alert)`,
  `ameeshHost.meshes()`; nothing else crosses;
* the SSE stream of alerts can be held by the **main process**, to notify with
  the window closed;
* the API call starts from the renderer (CORS: the application's origin is in
  the mesh's list) or goes through the main process; the second avoids
  exposing the API to a `file://` origin and remains the recommended choice;
* **no `<webview>` tag** (discouraged by Electron); if an application prefers
  the standalone page, a `WebContentsView` with a **partition per mesh**
  (`persist:ameesh-<org>`) is enough.

**Nexlink web page**: the page imports the ES module (pinned version, SRI
integrity), declares the mesh's API origin in its `connect-src`, and supplies
the `tokenProvider` from the human's session. The mesh lists the page's origin
in its allowed CORS origins.

**Third-party application**: same as Electron or a web page; it can also
ignore the components and build its interface on the OpenAPI alone.

## 2.4 Authentication and authorisations

* **Mesh in v2** (0033): the API is an **OAuth 2 resource server** that
  accepts access tokens issued by the **organisation's OIDC provider** (issuer
  and audience declared in the mesh configuration; checks of signature,
  expiry and audience; issuer public keys cached). The host application gets
  the token through the *authorization code* flow with PKCE, in the system
  browser for a desktop application (RFC 8252). Nexlink, if it is not the
  organisation's provider, connects to that provider like any client (the
  "Nexlink OIDC-compatible" ticket of 0033 §4).
* **Mapping**: the token gives a subject and an e-mail; the organisation's
  canon links them to `human:<id>`, to their roles, deputyships and
  superiors. A subject unknown to the canon has access to nothing.
* **Authorisations** on the server side, never on the component side:
  visibility of 0033 §9 (personas one is in charge of, deputy for or superior
  of; projects one is a member of), rule of 0029 for what touches persona
  repositories, roles for actions. The filters also apply to SSE events.
* **Local mode (v1)**: `ameesh serve` listens on the loopback, with a local
  token in a `0600` file (like ameesh-approve's service token); the human is
  the machine's owner, declared in the configuration. This mode serves the
  workstation and trials, not a team.
* **Approval**: it **remains a human passkey**
  ([0012](../decisions/0012-autorite-par-ameesh-approve.md)). The OIDC token
  identifies the human to read and act on operations; it is **never**
  approval, and neither is any delegated token (Nexlink, host application,
  digital double). The path: the human clicks **Approve**, the API files the
  request with ameesh-approve and returns the `/a/<token>` link, the
  component emits `ameesh:approval-requested`, the host application opens
  this link **in the foreground** (system browser, or the Nexlink
  application's native facade with its device key), the human signs on their
  team's origin `H`, and the receipt comes back through the stream
  (`receipt.attached`). The approval page keeps `frame-ancestors 'none'`: it
  is never displayed in a frame, so that the human sees the address of the
  origin asking for their passkey.

## 2.5 CSP, isolation and theme

* **CSP**: the components work under a strict CSP of the host:
  `script-src 'self'` (or the module's hash), no `eval`, no inline script or
  style (styles through constructed stylesheets, `adoptedStyleSheets`),
  `connect-src` limited to the origins of the meshes. Trusted Types
  compatible: no `innerHTML` of data; agent and thread texts are rendered
  **as text** (threads are already in fenced blocks, `fil.py`), with, at
  most, an allow-listed Markdown rendering that produces no raw HTML.
* **Isolation**: Shadow DOM for styles; no global state; one client per mesh,
  never shared; no access to the host page's storage.
* **Page served by `ameesh serve`**: same headers as ameesh-approve, with
  `frame-ancestors` limited to the declared integration origins (the
  standalone page can be displayed by a `WebContentsView`, not framed by
  anyone).
* **Theme**: public CSS tokens (`--ameesh-color-page`, `--ameesh-color-ink`,
  `--ameesh-color-good`, `--ameesh-color-critical`, `--ameesh-font`,
  `--ameesh-radius`…) taken from the palette of `progress_page.html`, light
  and dark; `theme="auto|light|dark"` attribute; `::part()` for the
  structures (header, card, button); the theme inherits from the host without
  touching it.

# 3. Where the service runs, and the right mesh

* **On the organisation's mesh server** (0033 §2), next to the database: this
  is the nominal case. Manaty's mesh already runs on a VM; it is the first
  deployment site. Behind the organisation's TLS proxy, on a host name
  **distinct** from ameesh-approve's.
* **Locally** (v1, single workstation): loopback, or a Nexlink relay as for
  ameesh-approve (0026, option 1).
* **Discovery**: `GET https://<api>/.well-known/ameesh` returns the
  organisation, the API version, the OIDC issuer, the ameesh-approve URL and
  the languages. The host application keeps a **list of meshes** (one per
  organisation), entered by the human or supplied by the organisation.
* **Several organisations**: a human can belong to several organisations, but
  each mesh is **sealed** (0033 §1). The host application opens one **OIDC
  session per mesh**, holds one **client per mesh**, and each component is
  bound to a single mesh (`mesh` attribute). No call crosses two meshes; no
  server sees the other. A view that gathers (for example "all my decisions")
  is composed **in the human's application only**, with the organisation shown
  on each entry and nothing sent back from one mesh to the other. In
  Electron, a partition per mesh separates cookies and storage.

# 4. Link with Nexlink Compute

According to the Nexlink Compute study of 2026-10-10: the Nexlink desktop
application (Electron/TypeScript, signed and notarised on macOS, signed on
Windows, Linux packages, automatic update) carries the Compute runner in a
`utilityProcess`; a device publishes an offer signed by its device key, the
hub places jobs under a 900 s lease, including, later, **ameesh agent turns**
run by a Linux ameesh runner in the sandbox (rootless container, VM on Mac and
Windows). This depends on the runner API mediated by the mesh server (v2).

**What the provider sees.** They can read everything on their workstation
(0029); they must therefore **see** what runs on it, without being a member of
the organisation that borrows their workstation:

* the **"my workstation"** view (`<ameesh-this-device>`) reads the **local
  state** of the machine's runners, not a mesh's API: for each mesh served,
  the organisation's name, the persona, the project and the lot (identifiers
  and title), the current turn and since when, the sandbox's CPU, memory and
  network, and the Compute lease;
* this state is exposed by the runner on a **local channel** (machine socket,
  read-only, no network token), in an `ameesh-device/1` schema; the desktop
  application reads it through its main process;
* a member of the organisation who lends their own workstation also sees the
  usual screens through their mesh's API.

**Stopping.** Two levels, always available offline:

1. **clean stop**: the runner ends or interrupts the turn, releases its lease,
   the lot goes back per 0030; the mesh receives the event;
2. **immediate stop**: the Compute runner destroys the sandbox and withdraws
   the offer; ameesh sees the lease expire and treats the agent as stopped.

The stop button depends on neither the network, nor the hub, nor the mesh: it
is the control of the machine's owner.

**A single desktop application.** Proposal: the **Nexlink desktop application
embeds both Compute and the ameesh interface**, rather than two applications.
Reasons:

* a single signing, notarisation and update chain, already in place (the
  portability study, L69, proposes to reuse it);
* a single device identity, a single menu bar, a single notification; the
  provider sees what Compute places **and** what ameesh runs there in the same
  place;
* the web components fit in with no specific code (section 2.3).

This creates no dependency of ameesh on Nexlink (0003): the human without
Nexlink has the standalone page; another organisation integrates the npm
package into its own application; the `ameesh-device/1` local state is
readable by any application.

# 5. Front-end technical choices

* **Language**: **TypeScript**, compiled by **esbuild** (one binary, no
  webpack chain). The API client is generated from the OpenAPI document.
* **Components**: **Lit** (BSD-3 licence, about 5 KB, custom elements
  standard, maintained for years, no proprietary compiler). Alternative with
  no dependency at all: hand-written custom elements, like the `progress`
  page; more code to maintain for reactivity and templates. Rejected: React,
  Vue, Svelte (impose a model on the host or a specific compilation).
* **Size budget**: under 80 KB compressed for the whole, checked in CI.
* **Delivery from Python**: sources in `ui/`; the built module
  (`ameesh-ui.js`, `ameesh-ui.css` of the tokens) is **committed to the
  repository** under `src/ameesh/ui/static/`, with a CI check that the build
  is reproducible and identical. This way `pip install` and `uv tool install`
  deliver the interface without Node, and a Python contributor does not need
  Node. The npm package is published from the same build, with the same
  version as ameesh.
* **Accessibility**: aim for WCAG 2.2 AA. Native elements (`button`, `table`,
  `details`) in the Shadow DOM; keyboard navigation and visible focus;
  `aria-live="polite"` for the conversation and alerts, with grouping so as
  not to saturate a screen reader; the timeline has a **table alternative**;
  contrasts checked in both themes; `prefers-reduced-motion` respected.
  Automatic tests (axe-core) in CI, and a manual pass with NVDA and VoiceOver
  before each major version.
* **Internationalisation**: **French and English** from the first lot. JSON
  catalogues per language in the module, plurals through `Intl.PluralRules`,
  dates and amounts through `Intl.DateTimeFormat` and `Intl.NumberFormat`.
  Language: the component's `lang` attribute, otherwise the document's,
  otherwise `GET /me`. The API returns **stable codes** (`working`,
  `unassigned`, `stopped_with_mail`…), already present in the JSON schemas,
  which the components translate; texts written by humans or agents (threads,
  messages) are never translated.

# 6. Points to align with the "resident agent" study

The parallel study plans an always-listening agent and `ameesh chat <agent>`
(terminal first, then a Nexlink page). For the interface to be compatible
with it:

1. **A single conversation protocol.** `ameesh chat` and `<ameesh-chat>` speak
   the same thing: the routes `POST /agents/{a}/chat` and
   `GET /agents/{a}/chat/stream`, or a shared Python module that the CLI
   calls locally. Proposed events: `chat.message` (human or agent, complete),
   `chat.delta` (fragment), `chat.step` (summary of a step: tool, file),
   `chat.turn_end` (cost, tokens, duration), `chat.state` (busy, free,
   interrupted). The native streams of the harnesses (Claude's stream-json
   events, Codex's app-server protocol) are **translated** by the adapter and
   never exposed as they are.
2. **Trace in the thread.** Each complete message, human or agent, is written
   into a thread (0006). Still to settle which one: the thread of the current
   lot, or a conversation thread per (human, persona). Fragments are not
   written.
3. **Identity of the interlocutor.** The human who speaks is the one of the
   OIDC token (or the owner in local mode), never inferred from the directory
   (0030 §6). The agent receives `human:<id>` in the message.
4. **Concurrency.** Two humans talk to the same agent, or the agent is in the
   middle of a lot turn: waiting queue, interruption, or a conversation
   session separate from the lot's session (0025, one session per lot)?
5. **Permission requests from the harnesses.** Resident harnesses can ask for
   a permission (tool, command). This is **not** ameesh's authority: either
   the sandbox policy settles them with no human, or the request is shown in
   the conversation and the human answers it through their session; but
   everything irreversible or costly remains a gated action, with a passkey.
6. **Cost.** Conversation turns enter the ledger (`turn_costs`) and the caps
   (0019, L70), and the screen shows it.
7. **Wake-up.** At tier 1 (no resident), the screen says clearly that the
   answer will come at the next turn; the API returns the agent's wake-up
   state (0030) to prevent a human from writing to an agent that will not wake
   up.

# 7. Proposed lot plan

Lots numbered from **L84** (L60–L74 taken, L75–L83 reserved for the resident
agent). A migration is to be reserved for `ameesh_events` when L85 opens,
after those of the lots in flight. Each lot: branch `lot/L8x-…`, tests,
independent review, merge on the owner's agreement.

| Lot | Content | Depends on | Size |
|---|---|---|---|
| **L84** — read API, local mode | `ameesh serve` (stdlib, ameesh-approve's defences factored into a shared module); `/api/v1` read routes on the existing serialisations (`projects`, `progress`, `list`/`show`, `decisions`, `alerts`, `hosts`, `budget`, `fil`); local mode (loopback, `0600` token); `GET /.well-known/ameesh`; generated and committed OpenAPI 3.1, conformance tests; systemd unit | L62, L70 | M |
| **L85** — event log and SSE | `ameesh_events` migration (append-only) and writes in the same transactions; trigger `pg_notify('ameesh_events', id)`; `GET /api/v1/events` (fetch-SSE, `Last-Event-ID`, heartbeat, connection limits); retention per 0033 §9; works with psycopg and psql | L84 | M–L |
| **L86** — OIDC identity and authorisations | token verification (issuer, audience, cached JWKS; ES256 through `p256.py`, RS256 through the `crypto` extra); subject → `human:<id>` mapping through the canon; roles, deputies, superiors; visibility filters on reads **and** events; CORS through an origin list; trials with Keycloak and Supabase Auth | L84, human roles in the canon (0033 phase 2) | L |
| **L87** — human actions | POST routes (message, thread publication, pause, resume, interrupt, stop with reason, reassignment, alert acknowledgement, host drain, cap lowering, filing of an approval request that returns the link); `Idempotency-Key`; "by X for Y" log; test: no route produces or attaches a receipt | L84; L86 for team mode | M |
| **L88** — front-end base | `ui/` (TypeScript, Lit, esbuild); client generated from the OpenAPI; SSE reader; `<ameesh-config>`, `<ameesh-app>`; light and dark theme tokens; fr and en catalogues; axe-core in CI; reproducible build committed under `src/ameesh/ui/static/`; standalone page `/ui/` | L84 | M |
| **L89** — read screens | `<ameesh-projects>` (L62 view), `<ameesh-timeline>` (port of the timeline of `progress_page.html`, with an alternative table), `<ameesh-agent>`, `<ameesh-alerts>`, `<ameesh-hosts>`, `<ameesh-budget>`, `<ameesh-thread>`; live updates | L88, L85 | L |
| **L90** — decisions and approvals | `<ameesh-decisions>` (R17); "Approve" path: filing, `ameesh:approval-requested` event, foreground opening, receipt through the stream; `app` channel in `ameesh notify` | L87, L89, L27 | M |
| **L91** — conversation | `<ameesh-chat>` tier 1 (mailbox + thread, wake-up state); tier 2 on the shared protocol with `ameesh chat` | L88, L87; L75–L83 for tier 2 | M |
| **L92** — package and integration guides | npm package `@ameesh/ui`; host contract (`ameeshHost`); Electron guide (preload, partitions, notifications, typical CSP); web page guide; example application for a fictional organisation | L89 | S–M |
| **L93** — "my workstation" | `ameesh-device/1` schema exposed by the runner on a local channel; clean and immediate stop; `<ameesh-this-device>`; contract with the Compute runner of the Nexlink desktop application | L88, L63; runner API v2 and NX-COMPUTE-3 for the Compute case | M |

Recommended order: L84, then L85 and L88 in parallel, then L87 and L89; L86 as
soon as the roles are in the canon; then L90 and L92; L91 follows the resident
agent; L93 follows Compute. L84, L85, L88 and L89 already give, in local mode
or on Manaty's mesh, a real-time view readable on a phone.

**Nexlink side** (tickets in the Nexlink repository, not ameesh lots):
integrate `@ameesh/ui` into the desktop application and into the project page;
implement the host contract; hold the OIDC sessions per mesh; plug the
`ameesh-device/1` state in next to the Compute runner.

# 8. Proposed decision

```markdown
---
type: Decision
title: "A human interface for ameesh: versioned public API and embeddable web components"
description: "ameesh exposes a v1 API (read, actions, SSE events) documented in OpenAPI and web components with no imposed framework, also served as a standalone page; one service per mesh, separate from ameesh-approve; OIDC identity of the organisation; an approval remains a passkey."
status: proposed
tags: [interface, api, sse, web-components, nexlink, compute, oidc]
decided_by: human:smichea
decision_date: 
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T20:00:00+02:00" }
sources:
  - { resource: "../etudes/ui-integrable.md", title: "Embeddable interface study" }
---

# Decision (00xx)

1. **Public API.** ameesh provides `ameesh serve`, a `/api/v1` API for reads,
   actions and events (Server-Sent Events fed by LISTEN/NOTIFY on an
   `ameesh_events` log), described in OpenAPI 3.1. The CLI's JSON outputs and
   the API's responses are one single serialisation.
2. **Web components.** The interface is a set of custom elements, with no
   framework imposed on the host, published as an npm package and served by
   `ameesh serve`; the standalone page is their default assembly. No iframe.
3. **One service per mesh**, separate from ameesh-approve (process, user,
   database role, origin). It runs on the organisation's mesh server, or
   locally on the loopback.
4. **Identity.** In a team, the API accepts the tokens of the organisation's
   OIDC provider; the canon links the human to their roles. Authorisations
   are applied by the server, including to events. A human in several
   organisations has one session and one client per mesh; nothing crosses two
   meshes.
5. **Authority.** The API has no approval route. Approving opens the team's
   ameesh-approve page in the foreground; no token, delegated or not, is
   approval (0012).
6. **A single desktop application** for Nexlink: it embeds Compute and the
   ameesh interface; the provider sees and stops what runs on their
   workstation, through a local state readable by any application.
7. **Front end**: TypeScript, Lit, esbuild; built module committed to the
   repository and shipped in the Python package; French and English; WCAG 2.2
   AA.

# Consequences

- Lots L84 to L93; one migration reserved for `ameesh_events` (L85).
- Nexlink tickets: integration into the desktop application and the project
  page, host contract, OIDC sessions per mesh, `ameesh-device/1` state.
- Licence of the component package: to be settled (question 1 of the study).
```

# Owner's choices (2026-10-10)

* **Licence**: the interface package stays under **AGPL-3.0-only** like the
  rest of ameesh ([0013](../decisions/0013-licence-agpl.md)); no permissive
  licence. A third-party organisation that embeds it in a proprietary
  application receives a written **exception**, granted case by case by the
  owner, outside the repository. Condition: the contribution agreement (CLA)
  of 0013 must cover all contributions, so that the exception is possible.
* **A single desktop application**: the Nexlink application embeds Compute and
  the ameesh interface.
* **ameesh never imposes Nexlink** (reminder from the owner, in line with
  [0015](../decisions/0015-interfaces-des-facades.md)). The Nexlink
  application is only **one** integration among others. Every function of the
  interface must stay reachable without it: through the command line, through
  the public API, and through a reference client that does not depend on
  Nexlink (the standalone page `/ui/`, or another client to be defined). No
  lot may make an ameesh function reachable only through the Nexlink
  application. Nexlink's choices (OIDC, approval, notifications) remain
  options among others. The reference client is the **standalone page
  `/ui/`**, kept (owner's decision, 2026-10-10).
* **Priority**: the interface lots move forward **together with** those of
  portability (L63–L69) and of the resident agent (L75–L83), in the same wave.

# 9. Questions for the owner

1. **Licence of the component package.** ameesh is under AGPL-3.0-only
   ([0013](../decisions/0013-licence-agpl.md)). AGPL components embedded in
   another organisation's desktop application risk blocking the integration.
   Keep the AGPL (the API stays usable without the components), or publish
   `@ameesh/ui` under a more permissive licence (Apache-2.0, MIT, or LGPL)?
   This assumes that Manaty holds the rights to the interface code (CLA,
   ownership of code written by agents).
2. **Lit or zero dependency** for the components?
3. **A single desktop application**: confirm that the Nexlink application
   embeds the ameesh interface and Compute, and that, without Nexlink, the
   installable standalone page is enough (no separate "ameesh desktop").
4. **Host names**: a team's API on a name distinct from `H` (ameesh-approve's),
   for example `api.<H>`, or on a name freely chosen by the organisation?
5. **What requires a passkey** beyond gated actions: raising a cap, forcing
   the assignment to an external agent, draining a host, stopping an agent?
   The proposal is: raising a cap and forcing an assignment, yes; the rest,
   no.
6. **Gathered view across organisations** ("all my decisions"), composed only
   in the human's application: acceptable with regard to the sealing of 0033,
   or must an organisation be able to forbid it?
7. **First deployment**: on Manaty's mesh (VM) in team mode as soon as L86, or
   local mode first?
8. **Conversation**: lot thread or a conversation thread per (human,
   persona)? And the permission requests of resident harnesses: shown to the
   human or settled by the policy alone?
9. **Approval in the desktop application**: always the system browser, or an
   application window when the system allows it (Electron's WebAuthn support
   to be checked per OS)?
10. **Priority**: where to place L84–L89 relative to portability (L63–L69) and
    the resident agent (L75–L83)?
