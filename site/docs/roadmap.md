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
