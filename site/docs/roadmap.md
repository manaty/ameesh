# Status and roadmap

## v1.0.0: done

v1 is the first version meant to be put in service on a real project. All its
lots were merged after review by another vendor, the end-to-end test passes
without workaround, and the full suite is green on both database drivers
(`psql` and `psycopg`).

- **Storage interface**: all SQL behind named operations, Postgres driver.
- **Canon**: reading at the merged commit, validation, sync to the registry,
  required responsible human, ephemeral agents.
- **Governed placement**: host policies in the claim predicate,
  `ameesh placement check`.
- **Readable threads**: `file` transport, unreadable bodies refused.
- **Actions and the gate**: durable states, stable identity, idempotency key,
  unknown outcome and reconciliation, `shell-noop` and `git-merge`
  connectors, `ameesh decisions`.
- **Receipts**: `ameesh-receipt/1`, WebAuthn, Ed25519 and device ES256 facades,
  trust registry in the canon, bounded standing approvals.
- **ameesh-approve**: approval and enrolment pages, request and receipt API.
- **Turn-based orchestrators**: events with coalescing, `ameesh attach`.
- **Switch plan** from v0, step by step and reversible.

## Since v1.0.0: built

These lots are built and reviewed on the development line:

- priority **interruption** of a turn, **session rotation** with a resume
  summary, sessions robust to a moved working directory;
- **cost accounting** per turn, subscription gauges, `ameesh cost`;
- **budget guard** before each turn, model and effort per harness,
  `ameesh set`;
- **review classes** by file scope in the canon, delay metrics per lot;
- **model catalogue** and discovery without spending tokens, `ameesh models`;
- **real-time progress view**, `ameesh progress` (text, JSON, standalone HTML);
- **cluster profile**: runner image, example Kubernetes manifests, read-only
  supervisor roles;
- **operational parity with v0**: session policy per agent with rotation when
  the lot changes, `ameesh list --json` (schema `ameesh-agent/1`), cost per
  turn, history of plan gauges, provider balance and real spend, alerts
  (`ameesh alerts`), `ameesh restart --brief`, `ameesh interrupt`, service tier
  in `ameesh set`;
- **ameesh-approve per team**: strict profile by default, distinct public and
  API addresses, `ameesh approve-check`, optional local TLS behind a
  passthrough gateway, a read-only database role per team, acceptance tests
  with two teams.

## v1.3.0 and v1.3.1: host resources and working directories

- **host resources**: each runner publishes memory, swap, load and disk
  readings, `ameesh hosts`; limits in `policy.resources` of the `Host` card;
  back-pressure before each turn (new turns held, lowest-priority agents
  paused under critical pressure, never mid-turn); orphan turn resources
  reported (`host_pressure`, `orphan_resource` alerts);
- **admissions**: a `Placement` lists admitted hosts or host tags, without a
  working directory; optional move to another admitted host between two turns
  (`AMEESH_RELOCATE`); persona visibility rule for memory repositories;
- **working directories from the host**: `policy.work_roots`, `work_root`, and
  in v1.3.1 `policy.work_dirs` with the `{agent}` template; a transitional
  fallback to the old placement `cwd` (`admission-cwd-inherited`, removed in
  v1.5.0 at the latest); a missing directory blocks the agent with a single log
  line and it resumes by itself when the directory comes back.

## v1.4.0: no work without a way to wake the agent, several canons

Design decision 0030, **no work without a way to wake the agent**:

- explicit **agent mode** `execute` or `externe` (`ameesh set <agent>
  mode=…`), structured **stop reason**; the runner never claims an external
  agent;
- **guarded assignment**: `ameesh work add --assignee` and `ameesh work
  assign` give lots only to agents ameesh can wake; **delegation with a
  deadline** (`ameesh work delegate --within`, `ameesh work
  expire-delegations`): an untouched lot goes back to the delegator;
- **adopt and resume** as ameesh operations: `ameesh adopt`, `ameesh resume
  [--fresh] [--brief]`, with the session's account of origin and a
  deterministic resume brief;
- new alerts `stopped_with_mail`, `orphan_lot`, `delegation_expired`, and
  **pushed alerts** to the responsible human with `ameesh notify` (desktop,
  ntfy, Slack; secrets only from the environment or a `0600` file; systemd
  user unit);
- **explicit session bindings** (`ameesh mail bind|unbind|bindings`): an
  identity never comes from the folder; `send all` reaches only the sender's
  team; `ameesh list` shows a LOTS column.

Design decision 0031, **several canons on one host**:

- a list of canons (`canons`, `AMEESH_CANONS`), the first being the default;
  a canon identified by its federation id; prefixed references for the other
  canons;
- each sync touches only its canon, with a canon state per (host, canon);
  names global to their first declarer; humans resolved in the agent's canon;
- a `Host` card per canon, with the strictest physical limits applied;
  authenticators tracked per canon;
- `extensions.ameesh.scope` in `federation.yaml` to bound ameesh cards to a
  folder of a shared canon (the older top-level `ameesh:` key is still read,
  with a warning);
- `ameesh canon check|show|sync --canon <id>`.

Also: `ameesh …` exits quietly with code 141 when the reader closes its output
(`ameesh alerts | head`). Upgrading needs migrations 0030 to 0036 on all hosts
at once: see [Upgrading an existing installation](guides/switch-from-v0.md#to-v140-migrations-0030-to-0036).

## Planned

In no particular order, and without dates:

- **provisioning** of the hosted modes of ameesh-approve (relay, dedicated
  page) and the gateway on the hosting side;
- **evaluation** of models on reference tasks, and recommendations;
- **harness descriptors** in a standard manifest format, a generic adapter, a
  harness catalogue and conformance bench;
- **signal intake** and a bounded **self-repair loop** (reproduce first, guarded
  diffs, bounded QA, escalation to humans);
- more **transports** for threads (team chat, e-mail) behind the same
  interface;
- the gate exposed as an **MCP proxy**;
- a **SQLite** driver for a personal, single-machine profile;
- **one Unix user per agent** and provenance signatures on agents' messages;
- an **installer driven by the user's own harness**.
