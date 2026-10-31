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

## v1.4.1 and v1.4.2: two patches

- v1.4.1 (L48): a harness that is missing, or that exits at once, is no longer
  relaunched every 5 s for ever. A turn that fails in less than 60 s is a
  **fast failure**: the wait doubles from 5 s to 300 s, and after five in a row
  the agent stops with `stop_reason` `erreur`, the instruction stays pending
  and the stopped-agent alert notifies the person in charge. `ameesh resume`
  restarts it once the cause is fixed.
- v1.4.2 (L49): the hourly cap on pay-per-token spending
  (`budget_usd_per_hour`) no longer pauses an agent on a subscription plan
  (Claude, Codex). Before, the spending of DeepSeek agents also paused the
  Claude orchestrator. An unknown harness stays subject to the cap
  (fail-closed).

## v1.5.0 and v1.5.1: sessions, speed and the database budget

Version 1.5.0 (migrations 0041, 0042):

- **L60**: DeepSeek turns record their real session, usage and model — no more
  "unknown" model billed at the pro rate — and a context ceiling
  (`context_max_tokens`) rotates the session;
- **L61**: psycopg becomes a dependency and reads are grouped. Against a remote
  database at 183 ms, `work list` goes from 19 s to 2.3 s and `cost report`
  from 83 s to 2.3 s; `cost gauges` and `progress` no longer write;
- **L62**: per-project view — `ameesh projects`, a PROJECT column in
  `ameesh list`, the projects view at the top of `ameesh progress`;
- **L70**: budget in the database for the whole mesh — `ameesh budget`,
  `budget set` and `budget unset` manage hourly and daily caps, global or per
  agent; runners reload them live and each change is logged;
- **L71**: accounting accuracy — the harness is read from the registry (which
  restores the Claude gauges), `accounts list` and `cost report` are read-only,
  the cost of a resumed session is counted as a difference, the balance is read
  once for the whole database, and a reading whose window has expired counts as
  0 %;
- **L72**: the runner survives a transient database outage and the current turn
  survives within the lease margin;
- **L74**: decision 0034 — ameesh first uses the plan capacity that expires
  soonest, and `accounts list` shows the capacity that will be lost at the next
  reset.

v1.5.1 fixes `ameesh projects` and the projects view when an agent has a
non-numeric session lot: the numeric conversion now happens in a `CASE`, since
Postgres does not guarantee the evaluation order of a `WHERE`.

## v1.6.0 to v1.6.5: housekeeping, planning and account spread

Version 1.6.0 (migrations 0043 to 0047):

- **L73**: ameesh cleans up what its agents create — a temporary directory per
  agent and per session, worktrees removed at the end of a lot, labelled
  containers, `ameesh menage`, alerts `tmpfs_full`, `worktree_kept`,
  `tmp_orphan`;
- **L94**: under-use alerts as much as overload — `plan_underused`,
  `idle_capacity`, `orchestrator_held`, `host_underused`, `balance_low`;
- **L95**: an accurate progress page and budget — pay-per-token spending and
  plan value never added together, real spending taken from balances,
  `ameesh cost correct`;
- **L96**: roadmap and planning — planned dates, dated commitments, Gantt,
  `ameesh plan`, `ameesh work plan`, alert `engagement_overdue`;
- **L105**: caps also act during a turn — maximum duration and bounded mail per
  turn;
- **L106**: surviving a host outage — harness binaries resolved without the
  session PATH, "host not ready" instead of fast failures, battery and mains
  power, resumption of a dead `attach` session.

Then:

- **v1.6.1**: the same non-numeric session lot fix as 1.5.1, in
  `operations.listing` (`ameesh list`, projects view);
- **v1.6.2**: amendment to decision 0034 (L117) — for a new session or a
  rotation, ameesh picks, among the usable accounts, **the one furthest behind
  its pace**; an account with no reading, or whose windows have expired, counts
  as 0 % and goes first;
- **v1.6.3** (migrations 0048 to 0050): waiting on host pressure shows in
  `ameesh show` and `ameesh list` (L31b); the platform layer and fail-closed
  adopt (L63); the continuous improvement queue, picked up by idle agents
  (L119); lots linked to mail and branch merges detected (L118);
- **v1.6.4**: fixes from the analysis of the night of 10/10 — resume summary
  without the owner's authority, tracking of lots merged outside a PR, mail
  refused to an unknown or stopped agent with alert `mail_undeliverable` and
  `mail forward`;
- **v1.6.5** (migration 0051): the end of a turn and a restart no longer kill
  work in progress (systemd `KillMode=mixed`), orchestrator priority under
  pressure, `turn_mail_max` 20, and each lot keeps a GitHub issue (L126).

## v1.7.0 and v1.7.1: chat, decisions and grouped mail

- **L123/L124**: `ameesh chat` and the central queue of decision requests
  (`ameesh decide ask`, `ameesh decisions`, `ameesh decide <id>`);
- **L125**: mail grouped before waking an agent (90 s window), passive
  acknowledgements, copies and broadcasts, alert `turn_churn`.
- v1.7.1: the hook no longer delivers an agent's mail to its subagents (L133).

No migration in either version.

## v1.8.0: migration numbers, durations and routed alerts

- **L122**: migration numbers reserved by lots, duplicates refused;
- **L157**: estimated duration of each lot, actual duration measured, gaps for
  the auditor;
- **L132**: alerts routed to the orchestrators and the auditor, with the
  expected action; **L130**: every request from a human becomes a lot;
- **L160b**: conflict radar in observation mode, `ameesh conflicts`;
  **L159c**: memory cap per agent and slice `ameesh-agents.slice`;
- **L135**: mesh server profile and switchover; **L146**: `doctor` recognises
  the VM's system units and writes nothing;
- **L151**: a deployment migrates before installing the new code;
  **L162**: nothing heavy in tmpfs, abandoned `/tmp` directories cleaned up.

Migrations 0052 (durations) and 0053 (reserved numbers), additive. Reinstall
`deploy/systemd/ameesh-runner-agent@.service` and
`deploy/systemd/ameesh-agents.slice`, then `systemctl --user daemon-reload`.

## v1.9.0 and v1.9.1: planning, CI feedback, radar and context ceilings

- **planning and estimates**: L157b publishes a reference estimate (median and
  p80 per lot type, read when a lot is created); L157c makes planned dates use
  the p80 and ordering the median, measures actual time per phase and size on
  the diff; L165 lets an idle agent take an unassigned lot of its team by
  itself;
- **CI and tests**: L161c brings every failing test to the lot agent's mail in
  real time; L161e measures and regenerates the test → code map on every merge
  to main; L161f selects tests from the measured map, impacted tests first;
- **conflict radar**: L160c makes the radar's mail passive and linked to the
  lot; L160d replays prepared resolutions with `rerere` in the radar's clone;
- **sessions and rotation**: L166 (a child session is never the agent's
  session), L217 (the context ceiling counts the main thread only), L217b
  (rotation judged on the main thread), L218 (the rotation summary is carried
  over whole), L221 (a Claude `result` with no usage falls back as in L105);
- **housekeeping**: L219 no longer deletes the temporary directory of an agent
  that is in a turn, or of another mesh on the same machine.

Migrations 0054 to 0057 (reference estimates, CI test results, lot measures,
conflict resolutions), all additive; apply them with `ameesh migrate` before
installing.

v1.9.1 is a patch on `release/1.9`: `send --new-lot` inherits the sender's
project, then the recipient's, instead of creating a lot with an empty project,
and the correction guard rejects invalid runner/session identities before a
human fallback, with correction roles required to come from the agent's own
canon.

## v1.10.0: mediated executor, capacity guard, progress API and English docs

`pyproject.toml` is at 1.10.0 and that version is deployed on the mesh hosts;
the full release note is `docs/versions/1.10.0.md`. No `v1.10.0` tag or GitHub
release is published yet. The main lines:

- **mediated executor (path B)**: devices run agents through the mesh server
  without database, forge or provider credentials — contract 1.1, mediated
  storage and runner, single-use enrolment and revocation, host gate, model
  relay, server-side work repositories, executor image and signing helper
  (L107–L114);
- **capacity guard and host gate**: the shared host measure (L159d) and the
  guard contract shared with Nexlink Compute (L159a), the guard observing
  levels 0–4 and enforcing them through systemd, drain and stop (L159e), the
  disk guard on btrfs and every volume
  (L229), heavy suites capped per host (L131), host alerts muted for a bounded
  span (L254), fewer runner database sessions (L653);
- **progress and API**: `ameesh progress --serve`, the page in tabs with a
  Gantt and a Versions tab that shows the last published version and the next
  release (L304, L160f); the read API `ameesh serve` (L84) and the standalone
  `ui/` page with its own CSP (L88);
- **budget, accounts and credentials**: Claude prices on the real scale and
  DeepSeek flash at the provider's price (L225), critical agents switching
  account on their own with a reserve and an hourly cap (L242b), a credential
  sentinel per host (L226a), `auth_blocked`, failover to a healthy account and
  never overage (L226b–L226d), lots handed over when a Claude agent is paused
  on budget (L233b), a revoked relay key recovered as `auth_blocked` (L317);
- **agents, sessions and rotation**: session ids only from the harness (L224),
  interactive Claude sessions with `--permission-mode auto` (L223), harness
  versions per host (L120), Claude gauges kept on their account across
  truncated tails and a bounded shared gauge cache (L227), durable host
  handoffs at lot boundaries (L232a, L232e), `ameesh watch` (L121), agent
  naming from the canon (L243);
- **planning, lots and decisions**: grouped orphan notifications (L50),
  stale-lot alerts deferred for planned starts and open dependencies (L368),
  project kept on every creation path (#495) and a message sent with `--lot`
  filed in the lot's project thread (L118c), thread retention (L361),
  DSN passwords off command lines (L518), fewer CLI round trips (L521),
  migration reservations per repository (L443), owner decisions only through
  `ameesh decide` (L233);
- **personas**: persona cards, human deputies and superiors (L51), retained
  persona sessions (L52), bounded activity log (L59), neutral memory transport
  (L54), off-device session backups (L53), a shared conversation journal and
  cursor notifications (L78), resident protocols for Claude, Codex and ACP
  (L75);
- **documentation**: `site/` and the root docs moved to English in stages
  (L156), the French identifiers renamed with compatibility aliases (L148), the
  1.10.0 companion docs (L712).

Migrations 0058 to 0069 and 0359, 0361–0364, 0368, 0371–0373, 0380, 0384,
0388, 0390, 0392, 0395, 0398, 0402, 0410, 0412, 0413, all additive; apply them
with `ameesh migrate` before installing the new code.

## Generic core and modules (L98–L104)

The core now exposes a module registry, project activation through the canon,
a generic [work lifecycle](concepts/work-lifecycle.md), and domain vocabulary.
The built-in `dev` module supplies development commands and workspaces;
installed extensions add trusted Python implementations, while Pipeline cards
provide workflows as validated data. Compatibility aliases remain accepted.

See [Core and modules](concepts/core-and-modules.md),
[Write a module](guides/write-a-module.md),
[Write a Pipeline](guides/write-a-pipeline.md), and the
[configuration reference](reference/configuration.md#modules-and-workspace-policy-in-the-canon).
These pages describe the implemented surface; they do not claim that the
remaining boundary-closure work of L104 is complete.
