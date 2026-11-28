# Status and roadmap

This page lists what each version delivered, up to **1.67.1**, then the
workstreams in progress. The full release notes are in `docs/versions/`.

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
  guard contract shared with an external compute service (L159a), the guard observing
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

## v1.11 to v1.67: one entry per minor version

Since v1.12 the mesh has released at two fixed cuts a day (11:00 and 23:00,
Europe/Paris), and each cut is tagged and deployed to staging. Every release
note is in `docs/versions/<version>.md`. The version deployed when this page was
written is **1.67.1** (21/11/2026). Patch releases are listed with their minor
version.

- **v1.11** (15/10/2026; patches .1–.7): first release for an installation
  outside the home mesh. Adds daily budgets shared by all agents on one
  provider account (#1469). CI on lot branches now runs only impacted and
  contract tests, and scripted batches integrate every hour behind a packet
  guard (#2333). Adds opt-in Process workflows with a compiler, registry and
  verifiers (#1918, #1922, #1924), operator pause and resume of agents (#1462),
  the automatic batch train (#1467) and `ameesh work landing` (#1945).
- **v1.12** (18/10/2026): adds review after merge, done by the other model
  family, and requires two reviews to merge sensitive lots (#1280, #2377,
  #2374). Reviewers are designated at freeze (#2447). Migration numbers can be
  reserved in several slots (#711), and the hourly composition is built from
  computed ready lots (#2404).
- **v1.13** (19/10/2026): adds `ameesh agent peek` (#1398) and
  `ameesh release packet-watch`, which composes a batch at every hourly slot
  (#2601). Also adds `pause --not-after`, several migration reservations per
  lot (#2481) and a mechanical guard on cut hours (#2449, #2457).
- **v1.14** (19/10/2026): adds `ameesh work wake` and
  `work note --wake-at/--wake-in` (#2466, #2613). At freeze, the tests mapped to
  the diff run on a VM (`ci run --prevalidation-map`, #1770, #2644).
  Reviewers are now designated by model-family pace (#2668).
- **v1.15** (20/10/2026): adds `ameesh work packet record/show` (#2443).
  ameesh merges a published packet itself, behind a full verdict and
  required-checks gate (#2813). Corrections must show their review provenance
  (#2739), and prevalidation passes are bounded (#2665).
- **v1.16** (20/10/2026): admission takes cgroups into account and uses a
  pluggable elastic capacity provider (#2814, #2825). A new GitHub watcher turns
  external issues and comments into lots, notes or replies (#2845). Review
  authorship is now read from the frozen commit range (#1515, #2808).
- **v1.17** (21/10/2026; patch .1): adds `ameesh device view/stop` and
  `ameesh process check` (#2922). Module descriptors can come from external
  packages (#2847), and spend is keyed exactly per turn (#2893). Adds a resident
  input queue (#286) and an alert when the shared GitHub observer falls silent
  (#2951).
- **v1.18** (22/10/2026): adds `ameesh work neighbors` (#2988),
  `work add --reviewed-head` and cost per resident message
  (`cost turns --message`, #288).
- **v1.19** (23/10/2026): `ameesh chat --veille` watches decisions and mail
  between the owner's messages (#480). The full suite also runs on macOS
  (#393).
- **v1.20** (23/10/2026): pilot election and handover between candidate hosts
  (#3010). Alerts and escalations are routed by liveness (#3036). Adds
  `work milestone --supersedes` (#2989), and the macOS suite becomes required
  (#3027).
- **v1.21** (24/10/2026; 140 lots): elected roles with epoch-fenced gestures,
  plus a train-service lease and failover (`ameesh role`, #3013, #3014, #3142).
  Also adds `ameesh agent move` (#3299), `ameesh debit` (#3292),
  `ameesh host reconcile` and `ameesh release staging-needed` (#3291).
- **v1.22** (25/10/2026): adds `ameesh role canary` to test failover (#3433,
  #3015), `ameesh work set-priority` (#3370) and train `requalify`. Dead, paused
  or ephemeral agents are no longer designated as reviewers (#3411, #3415).
- **v1.23** (26/10/2026): adds `ameesh agent retire/restore` (#3460) and
  `ameesh work external-executor` (#3447, #3498). Mail that only informs no
  longer opens a pilot turn (#3451). A release cut accepts a head whose tree
  matches a green packet (#3633).
- **v1.24** (26/10/2026): adds `ameesh handoff advance/cancel/retry`, run
  through a pinned privileged dispatcher (#3767), and `AMEESH_FREEZE_GATE`.
  Adds the `rate_limited_chronic` alert (#3733) and an account pace compiled
  from the canon (#3647).
- **v1.25** (27/10/2026): the test-bench database is purged of orphan schemas
  and its bloat is reported (#3735). Old virtualenvs are cleaned up after a
  staging deploy (#3792).
- **v1.26** (27/10/2026): hourly packet composition without an agent
  (`paquet-auto`, #3807), with measured parallel CI parts (#3754). Adds
  `ameesh process-approvals`, a bridge that fails closed (#4006), operator
  pauses that expire (#3961), idempotent mail replay (#3892) and a journal of
  server faults (#3885).
- **v1.27** (28/10/2026): backlog sizing with
  `ameesh work backlog add --estimate/--size` (#4117) and a prevalidation
  budget. The macOS suite runs as a CI matrix (#4063). Review verdicts survive
  conflict fixes on the target side (#4053).
- **v1.28** (28/10/2026): adds the module interface and registry
  (`ameesh modules`, #4308). Each lot gets a footprint, and lots with
  overlapping footprints are not scheduled together (`ameesh work footprint`,
  #4173). Adds `ameesh work coherence` and `ameesh work links`. Approval
  authority becomes generic, and a guard rejects client-specific literals
  (#4259).
- **v1.29** (29/10/2026): prepaid provider credits with `ameesh budget prepaid`
  and low-balance alerts (#4166, #4164). Harness and model versions are watched
  daily (#4291). Adds a comparative replay bench (#4292), Pipeline cards
  (#4395) and a second parallel packet when merges back up (#4275).
- **v1.30** (31/10/2026; 210 lots): modules can be activated per federation,
  team or project, and the interface uses the active module's vocabulary
  (#4407, #4391–#4393). Adds `ameesh agent rename` (#4299), validation
  deadlines with escalation (#4256), context counters (`ameesh-context/1`,
  #4829), `ameesh turn observe/current` (#4831, #4874) and impact analysis
  before dispatch (#4818).
- **v1.31** (31/10/2026): adds `ameesh agent review-hold` (#5087) and opt-in
  maintenance of the context window (#4971). Each turn publishes its effective
  context profile (#5146).
- **v1.32** (01/11/2026): `ameesh context corpus export/check` exports a replay
  corpus (#5134, #5147), and `ameesh sandbox list/show` inspects sandboxes
  (#5217). Adds a PostgreSQL major-version guard (#5189) and the
  `merge_lock_stuck` alert (#5179).
- **v1.33** (01/11/2026; patch .1): a context policy per agent with a
  reversible pilot (`ameesh context pilot …`, #5250). Packets that change only
  docs skip CI when proven on their real diff (#5428).
- **v1.34** (02/11/2026): proofs of refusal are emitted before a call
  (`ameesh refusal emit`, #5529). Clock and timezone are checked at freeze
  (#5427). Adds `AMEESH_FREEZE_PATHS`.
- **v1.35** (03/11/2026): `ameesh budget reconcile` (#5503). Adds backoff when
  a session rotation fails (#5510) and disk monitoring of the server host
  (#5559). The size of the canon rules block is checked before merge (#5556,
  #5562).
- **v1.36** (03/11/2026): forecasts epic delivery from run capacity (#5595) and
  replans automatically (#5602) with `ameesh work plan --capacity-plan`. Agent
  places can be reserved per project or class (#5601).
- **v1.37** (04/11/2026): adds `ameesh doctor --all-bases` (#5692) and
  `--wait/--cancel-ci/--expect-head` for packet recomposition (#5696). Adds a
  bench for an old writer with a new reader (N-1, #5690). The watcher opens a
  turn only when something changed (#5698).
- **v1.38** (04/11/2026): CI checks disk headroom first (#5742) and reuses a
  packet's qualification when the tree is identical (#5729). Adds an installer
  for a server with rootless Docker (#5784).
- **v1.39** (05/11/2026): turn preparation is bounded
  (`AMEESH_MAX_PREPARATION`, #5826). Adds `ameesh work sync-merges --id` and a
  versioned hourly auditor pass (#5863). Closed hosts no longer receive work
  (#5867).
- **v1.40** (05/11/2026): an asynchronous VM lifecycle through a VM adapter
  (#5925, #5932). Adds host cost per state with reserved-capacity alerts
  (#5912), a daily history of host sizing (#5921) and fingerprint passes per
  environment (#5968).
- **v1.41** (06/11/2026): adds `ameesh pause/resume --actor` (#5967). Writer
  admissions are time-bounded, and red CI bases are remembered (#5980, #5971).
  Each host gets a staging probe seat (#6013).
- **v1.42** (06/11/2026): adds `ameesh cost attribution` (#6005) and
  `ameesh release attest` (#6020). Context compaction thresholds can be set per
  agent or role (#6040, #6044, #6046). A missed release cut raises an alert
  (#6014).
- **v1.43** (07/11/2026): external executors that are not agents are declared
  with a human owner (#6091). The footprint can be read from the branch on the
  forge (#6092). Adds `ameesh cost turns --over` (#6045). Each agent runs one
  turn at a time (#6134).
- **v1.44** (07/11/2026; patch .1): adds `ameesh work add --unique-ref`
  (#6090). Tests get a contract registry, a contract pass on the composed tree
  and versioned attestations (#6179, #6182, #6184). Attestations of an exact
  tree are reused (#6274).
- **v1.45** (08/11/2026): the first self-repair loop, with a journal of repair
  signals and the read-only `ameesh repair-report` (#6313–#6332). Adds health
  receipts for VMs (#6298, #6329).
- **v1.46** (09/11/2026): mailbox read indexes give exact unread counts
  (#6422, #6424). Canon updates are broadcast as a bounded diff (#6425). The
  merge gate counts author families only on the lot's own commits (#6365).
- **v1.47** (09/11/2026; patch .1): adds `ameesh work packet check/hold`
  for composition lease checks. `export-v0` now requires an explicit scope (#6534).
  Automatic retries are journaled (#6475, #6487), and stacked branches that
  declare no dependency are detected (#6607).
- **v1.48** (10/11/2026): adds `ameesh model-report` and
  `ameesh work findings` (#6661, #6669, #6670), and `ameesh github quota`, which
  reserves quota for merge gates (#6649). Context caps can be set per harness
  (#6677).
- **v1.49** (11/11/2026): adds `ameesh work claim` (#6839) and
  `ameesh carte dispatch` (#6777). Staging requests can be cancelled and
  expire (#6776). Required checks are declared in the canon (#6832).
- **v1.50** (11/11/2026; patch .1): high availability for the central
  database: WAL archiving to another host with point-in-time restore (#6858),
  a private network (#6859), PostgreSQL 17 replication with Patroni (#6860,
  #6868, #6870), and a cluster-aware `ameesh db status` (#6871). Turns have a
  hard duration cap (#7008).
- **v1.51** (12/11/2026): adds `ameesh provenance revoke` (#6873). A migration
  that is only renumbered keeps its verdicts (#7000). Signed questions are
  checked for a covering signature at preparation (#7013).
- **v1.52** (13/11/2026): avatars: WebAuthn grants for remote attach
  (`ameesh attach-grant`, #7119), conversations that move between hosts
  (#7050) and a preferred host (#7048, #7052). Turns pause at the account
  ceiling (#6961). Adds database failover drills (#6862, #6863).
- **v1.53** (13/11/2026): shared hosts with several tenants
  (`ameesh host tenant …`, #7106). Child agents run with restricted tools and
  MCP (`ameesh child`, #7034). Adds `ameesh attach-relay` (#7120) and
  `ameesh accounts exception` (#7066).
- **v1.54** (14/11/2026): remote `ameesh attach` with grants, host and device
  keys, and TLS (#7121). Attach sessions are logged to the conversation log
  (#7047). CI runners recover by themselves after a failure (#7319, #7320).
- **v1.55** (14/11/2026): adds the `ameesh staging-migrations` preflight; any
  doubtful SQL needs the owner's agreement (#7368, #7392, #7405). Makes
  `ameesh alerts` much faster (#7350).
- **v1.56** (15/11/2026): accounts switch and rotate in waves at turn
  boundaries (#7441). Tool outputs and prompts are measured without being
  stored (#7468).
- **v1.57** (15/11/2026): each packet stores a snapshot of its exact
  composition (#7477, #7478, #7480). Adds `ameesh psql-nocturne`, a bounded
  nightly PostgreSQL CI (#7483).
- **v1.58** (16/11/2026): adds cycle budgets for expiring credit
  (`ameesh budget cycle …`, #7518, #7520) and `ameesh release catchup` (#7372).
- **v1.59** (16/11/2026; patch .1): adds `ameesh release abandon` (#7567),
  bounded catch-up of staging (#7565) and an alert on CI parts that were never
  dispatched (#7605). Projects can declare module connectors for actions
  (#7653).
- **v1.60** (17/11/2026): standing approvals bound to a beneficiary, a target
  and a ceiling (`ameesh action standing`, #7629), plus `ameesh action recap`.
  Adds `ameesh agent observe` (#7670), `ameesh canon snapshot` and
  `ameesh host reattach` (#7654). Humans can be reached through their avatar
  (#7634).
- **v1.61** (18/11/2026): a weekly stable channel
  (`ameesh release stable …`, #7666). Adds module schemas, phase 1 (#7649),
  and session policy per agent (#7674).
- **v1.62** (18/11/2026): `ameesh gate` audits actions before they run and
  issues single-use receipts. Adds the credential broker (#7761, #7767) and
  public contract 1.3/1.4 (#7672, #7673). Governance roles are read from the
  agent card (#7661).
- **v1.63** (19/11/2026): adds `ameesh action journal`, `ameesh receipt sign`
  and `ameesh standing list/register`, and avatars can sign decisions (#7631).
  Adds module schemas, phase 2 (#7650), and a public fixture kit in contract
  1.5 (#7701).
- **v1.64** (19/11/2026): adds `ameesh action standing request`,
  `ameesh responder` (#7866) and `ameesh action execute --proof` (#7684). MCP
  tool allow-lists can be made read-only (#7859). Idle attached sessions wake
  natively (#7863).
- **v1.65** (20/11/2026): lays the pod foundation, with a shared multi-tenant
  chart and a supervisor lease (#7694, #7695). `ameesh action spend` sets
  pre-delegated spending caps (#7865). Work fails over to the cloud when the
  workstation runner stops (#7833). Adds `ameesh host release-check` (#7874)
  and an alert for a stuck train (#7889).
- **v1.66** (20/11/2026): host capabilities, such as an Android emulator, can
  be declared, provisioned and verified (#7926). Adds a reversible cutover to
  module schemas (#7651). The CI queue no longer waits on GitHub capacity
  (#7935).
- **v1.67** (21/11/2026; patch .1): agents are cloned into pods with
  `pod-provisioner` and the `pod-admission` webhook (#7697). Agents can move
  between a workstation, a shared pod and a dedicated pod (#7698). Adds a
  reference chart for a pod host (#7699). Late mail is delivered first
  (#7976), and the N-1 preflight is part of the contract (#7985, #8004).

## Generated release entries

<!-- generated releases: begin -->
- **v1.69.0** (2026-11-22): Migrations 0604; +11 internal lots.
- **v1.70.0** (2026-11-23): +13 internal lots.
- **v1.71.0** (2026-11-23): +23 internal lots.
- **v1.72.0** (2026-11-24): Migrations 0607; +17 internal lots.
- **v1.73.0** (2026-11-24): Migrations 0606, 0608; +15 internal lots.
- **v1.74.0** (2026-11-25): Commands wait; +20 internal lots.
- **v1.74.1** (2026-11-26): +4 internal lots.
- **v1.75.0** (2026-11-26): Migrations 0609; +8 internal lots.
- **v1.75.1** (2026-11-27): +8 internal lots.
- **v1.76.0** (2026-11-27): Migrations 0610, 0611, 0612; Commands lineage, profiles, reservations; +20 internal lots.
- **v1.77.0** (2026-11-28): Migrations 0613; Commands prices; +19 internal lots.
<!-- generated releases: end -->

## In progress

These workstreams were open when this page was written. Their state is in the
lots.

- **Mesh standby** (#8026–#8031, manaty/ameesh#6266): when there is no
  activity, only the cheapest VM (the hub) stays on and carries the critical
  services. Delivered: the database standby mode (#8027) and the hub's `Host`
  card, which tracks its hosting bill in the budget (#8030). Validated: elected
  roles hosted on the hub at rest (#8028), and waking a CI VM when a job waits
  without a runner (#8031). To do: shutting down agent and CI VMs on
  `roadmap_exhausted` or after 30 minutes at rest, and waking them on the first
  ready lot (#8029).
- **Credential broker** (#7798): the broker and its installer are delivered
  (#7767). Applying it means a dedicated KMS account, a GitHub machine identity
  and removing direct credential paths. It waits for an owner decision.
- **OpenAI API account** (#5465, manaty/ameesh#6178): a study of how an OpenAI
  API key joins the mesh under a hard spending cap, written before any code.
- **Container admission**: an avatar host in a Kubernetes pod (#7633). Phases
  P1–P6 are delivered in v1.65–v1.67. The subscription part (#7911) waits for
  answers from the owner.

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
The boundary is closed (L104): the core imports no module implementation apart
from a declared, dated list of compatibility aliases, and a new canon starts
without any project module.
