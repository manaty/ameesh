# "cluster" hosting profile — one persistent agent per pod

This profile runs **one** persistent ameesh agent in a Kubernetes cluster.
It is one of the profiles planned by decision
[0011](../design/decisions/0011-hebergement-configurable.md) ("an existing
server or cluster"); the database follows the profile
([0016](../design/decisions/0016-stockage.md)): a durable Postgres, backed
up off the machine.

This is a **minimal** profile: the runner and its harness, nothing else.
Neither the gate with human receipts nor `ameesh-approve` is exposed; the
agent is declared in the canon with only the `read` and `propose`
capabilities.

Files:

| File | Role |
|---|---|
| [`Dockerfile`](../../Dockerfile), [`.dockerignore`](../../.dockerignore) | runner image, no secret, no harness by default |
| [`deploy/k8s/`](../../deploy/k8s/) | example manifests (kustomize): StatefulSet, ConfigMap, portless Service, NetworkPolicy, service account |
| [`deploy/k8s/secrets.exemple.yaml`](../../deploy/k8s/secrets.exemple.yaml) | shape of the expected Secrets — never filled in, never applied as is |
| [`deploy/sql/role-superviseur.sql`](../../deploy/sql/role-superviseur.sql) | read-only Postgres role for an external supervisor |

---

## 1. Architecture

```
                      canon repository (git)               durable Postgres
                      Host = pod name,                     (outside the pod: managed
                      Agent, Placement                     service or dedicated StatefulSet)
                             │ read-only (deploy                  ▲
                             │ key)                               │ AMEESH_DSN + PGPASSWORD
  ┌─ pod ameesh-agent-0 ─────┼────────────────────────────────────┼──────────────┐
  │  init migrate      ameesh migrate ───────────────────────────►│              │
  │  init canon        git clone ; ameesh canon sync --fetch ────►│              │
  │                          ▼                                    │              │
  │  canon-fetch  ──► emptyDir canon/ (periodic git fetch)        │              │
  │                          │ mounted READ-ONLY                  │              │
  │  runner        agent-runner ── periodic canon sync ──────────►│              │
  │   (tini, PID 1)   └─ harness (claude | codex | dsh)           │              │
  │                        HOME = PVC: sessions, state, threads, work directories│
  └──────────────────────────────────────────────────────────────────────────────┘
        no network ingress (portless Service, NetworkPolicy); egress: database,
        git, model provider API
```

* **Identity.** The pod name (`ameesh-agent-0`) is the ameesh host
  (`AMEESH_HOST`, via the downward API). The canon declares a `Host` with
  this name, with its responsible person and its policy, and the agent's
  `Placement` on this host (spec §4.2). Without them, the agent cannot be
  claimed (R14, C4: fail closed). One replica, always: two replicas would be
  two hosts.
* **Canon.** Cloned by the `canon` init with a **read-only** deploy key,
  then `ameesh canon sync --fetch`. Afterwards, the `canon-fetch` container
  runs `git fetch` every `fetch-interval` seconds, and the runner re-reads
  the canon every `AMEESH_CANON_SYNC_INTERVAL` seconds (without fetch: it
  does not have the key). The clone is mounted **read-only** in the runner's
  container; the key is never mounted there.
  `canon_ref: origin/main` in `config.json` sets the host's trusted canonical
  branch (spec §8.2): no `--bootstrap-ref` bootstrapping to do.
  Limitation: a single canon repository (federation members present
  locally, `workspace_path`, are not cloned by this example).
* **State.** The database holds the state (leases, mailbox, registry,
  actions). The `home` persistent volume holds what lives on the host:
  harness sessions (`~/.claude`, `~/.codex`…), ameesh local state
  (`~/.local/state/ameesh`, including the **readable threads**), work
  directories (the Placement's `cwd`, for example
  `/home/ameesh/travail/<project>`).
* **Shutdown.** tini relays SIGTERM to `agent-runner`, which stops the
  harness group (SIGTERM, 3 s, SIGKILL), joins its turns (8 s at most) and
  releases the lease. `terminationGracePeriodSeconds: 60` also covers an
  in-flight query up to the `statement_timeout` (30 s). Beyond that,
  Kubernetes kills the pod: the lease expires on its own (`lease_ttl`) before
  any takeover.
* **Probe.** `ameesh doctor --probe` as `readinessProbe`: database
  reachable, schema present, migrations up to date — read-only, neither
  NOTIFY nor counting. No `livenessProbe`: restarting the pod does not fix an
  unreachable database, and the runner already handles that failure; if it
  dies, the container exits and Kubernetes restarts it.

## 2. What is guaranteed, and what is not

The spec's table ([§11](../design/specification.md)) applies here, with
these differences:

| Guaranteed in this profile | Not guaranteed |
|---|---|
| **one agent per pod**: its file system, processes and sessions are isolated from other agents (another pod, another volume) — an isolation the local profile, where all agents share the Unix user, does not have | isolation **in the database**: the runner and its harness share ameesh's Postgres role (`PGPASSWORD` is in the harness's environment, as the `agent-mail` hooks require); the agent can read and modify the state of the other agents in the same schema |
| the canon is **read-only** for the agent; the deploy key is not mounted for it; it cannot rewrite the clone's `refs/remotes/…` (limitation §4.1 of the local profile) | the agent can write to the database (`canon_state`, registry) with ameesh's role: monotonicity and the claim predicate remain the only safeguards |
| read-only container root, no Linux capabilities, non-root user, no Kubernetes API token | network egress open in the example (database, git, provider): to be restricted depending on the cluster; no network sandbox nor MCP proxy (v1.x) |
| no network ingress: the **gate and `ameesh-approve` are not exposed**; an `irreversible` or `costly` action stays blocked for lack of a receipt (`[receipt_required]`) — the safe behaviour | no human approval possible from this profile: it requires an `ameesh-approve` deployed **out of the agents' reach** (spec §9), which this profile does not provide |
| an agent with no responsible person, misplaced, or whose canon is invalid is not claimed | the example does not check that the agent's capabilities are limited to `read` and `propose`: it is the canon's `Agent` card (reviewed by PR) that states it |

Expected agent card (excerpt, spec §4.2):

```yaml
type: Agent
title: veilleur
responsible: human:alice
capabilities: [read, propose]     # neither irreversible action nor approval in this profile
harness: claude
provider: anthropic
credential_mode: api-key
```

## 3. Setup (operator acts)

None of this is done by an agent or by the repository: creating the
registry, the database, the Secrets and applying the manifests are operator
acts (and any expense is a `costly` action, decision 0011).

1. **Image.** `docker build -t <registry>/ameesh-runner:<version> .`, with
   `--build-arg HARNESS_NPM=<package@version>` for a harness distributed via
   npm, or a derived image (`FROM` + `COPY` of the binary) for another one
   (header of the [`Dockerfile`](../../Dockerfile)). Pushed to **your**
   registry; report the name in `images:` of `kustomization.yaml`.
2. **Database.** A durable Postgres (managed service, or dedicated instance
   with volume and backups). A role for ameesh, owner of its schema
   (the `migrate` init applies the migrations — risk and planned follow-up in
   section 9). DSN **without password** in
   `config.json`; the password in the `ameesh-db` Secret.
3. **Canon.** `Host` (name = pod name), `Agent` (capabilities `read`,
   `propose`) and `Placement` (`cwd` under `/home/ameesh`) cards, merged by
   PR. Read-only deploy key on the canon repository, `known_hosts` verified
   out of band.
4. **Secrets.** The three Secrets of
   [`secrets.exemple.yaml`](../../deploy/k8s/secrets.exemple.yaml), created
   outside any repository (`kubectl create secret …` or secrets manager).
5. **Render then apply.** `kubectl kustomize deploy/k8s` (review it),
   then `kubectl apply -k deploy/k8s`.
6. **Harness.** On first start, HOME is empty: the configuration of the
   harness hooks (`agent-mail hook <harness>`, as on a workstation) and the
   work repository (the Placement's `cwd`) must be set up once, via
   `kubectl exec` or via a derived image. They then stay on the
   volume.
7. **Check.** `kubectl logs ameesh-agent-0 -c runner` (« bail acquis »,
   "lease acquired"), `kubectl exec ameesh-agent-0 -c runner -- ameesh canon
   check`, `ameesh list`, `ameesh doctor --notify-test`.

## 4. Read-only external supervisor

The supervisor may belong to **another organisation** than the agents'
team. By default, it therefore sees the mesh's **state**, not the **work**
itself: two distinct roles, both `NOLOGIN`, read-only.

* [`deploy/sql/role-superviseur.sql`](../../deploy/sql/role-superviseur.sql)
  — base role `ameesh_superviseur`: registry and `agent_mesh_overview` view
  (status, lease, unread, budget, placement), mailbox metadata (sender,
  recipient, type, status, dates), lots (title, state, assignee), thread
  index (without excerpt), actions and attempts (class, state, target,
  without arguments or notes), costs, canon state, migrations.
* [`deploy/sql/role-superviseur-contenus.sql`](../../deploy/sql/role-superviseur-contenus.sql)
  — role `ameesh_superviseur_contenus`, **explicitly granted in addition** to
  a supervisor authorised to read the work: message bodies, payloads and
  metadata (`agent_mailbox.body`, `payload`, `meta`), prompts
  (`pending_prompt`, `current_prompt`), pending restart brief
  (`restart_brief`, L26), thread excerpts
  (`thread_index.last_excerpt`), action arguments and notes (`actions.args`,
  `last_note`, `action_events.note`), lot descriptions and notes
  (`work_items.body`, `work_item_events.note`, `work_item_milestones.note`), approval metadata
  (`mesh_approvals.meta`). On its own, it only gives these columns.

Neither has any write right, sequence right, nor access to the signature,
receipt, nonce or challenge columns, to the public keys or to the
authenticator identifiers; the exact list of exclusions and their reasons
are at the top of `role-superviseur.sql`. Still readable by the base role,
as state: lot titles, action targets and short diagnostics
(`status_text`, `last_error`, `placement_diagnostic`…), which may quote an
excerpt of a harness error — remove them from the script if that is not
acceptable.

```bash
export PGOPTIONS='-c search_path=public'
psql "<admin DSN>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
psql "<admin DSN>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur-contenus.sql
# then one login role per supervisor:
#   CREATE ROLE supervision_outil LOGIN IN ROLE ameesh_superviseur;
#   ALTER ROLE supervision_outil SET default_transaction_read_only = on;
# and, only for a supervisor authorised to read contents:
#   GRANT ameesh_superviseur_contenus TO supervision_outil;
```

Each script opens **its own transaction** and guarantees its result, not
only its own statements. It creates the role if it is missing, revokes the
rights this role received directly, and grants read access column by column
per its contract (explicit list in the script). Then it **audits the
effective rights** of the role (`has_column_privilege`,
`has_table_privilege`…): those received directly, but also from `PUBLIC`,
from the roles it inherits from and from predefined roles
(`pg_read_all_data`…). It also audits the schema's default ACLs
(`ALTER DEFAULT PRIVILEGES`), which would open a future table or column to
`PUBLIC` or to the role. Finally, it checks the absence of write, sequence,
creation in the schema, executable `SECURITY DEFINER` function and inherited
privileged attribute.

**A supervisor role is a member of no role.** It may belong neither to a
parent role, nor to a predefined role, nor to the other supervisor role.
Even without inheritance (`INHERIT FALSE`), a membership would let a login
that is a member of the supervisor `SET ROLE` to that role and read what the
effective rights audit does not see. This is also the case for any
membership before PostgreSQL 16. Any membership is therefore refused. The
reverse direction is the only one planned: login roles are members of the
supervisor roles. The administrator makes them members of no other role;
this point belongs to their operations, the script does not create these
roles and does not audit them.

If the contract is not met, the script **refuses**: it raises an error that
lists each discrepancy, and the transaction is rolled back (no role created,
no right changed). It never tries to repair rights granted by others, to
`PUBLIC` or to a parent role: the administrator revokes them at the source,
then reruns. Under PostgreSQL ≤ 14, this includes the `CREATE` granted to
`PUBLIC` on the `public` schema.

The scripts are idempotent: rerun them **after every update** of ameesh. A
column added by a migration is only readable once named in a contract, and a
contract column missing from the database (database behind on migrations)
makes the script fail. The test `tests/test_cluster.py` applies them on a
real Postgres. It fails if a schema column is neither in the base role's
contract nor explicitly excluded, if the base role reads a content, or if
the "contents" role reads anything other than the contents. It also checks
the transactional refusal in three cases: rights granted to `PUBLIC`, right
inherited from a parent role, default ACLs — and two membership cases:
parent role reachable by `SET ROLE` without inheritance, predefined role
`pg_read_all_data`.

## 5. Backups

* **Database**: the managed service's backup, or a daily `pg_dump` copied
  **outside the cluster** (0011: backed up off the machine as soon as one
  leaves the local profile). Restore drill mandatory, as in step 1 of
  [`BASCULE.md`](../BASCULE.md): restore into a throwaway database,
  `ameesh doctor --probe` on it, delete.
* **`home` volume**: volume snapshots (VolumeSnapshot, if the storage class
  allows it) or periodic copy. It holds the harness sessions and the
  **readable threads**; their index is in the database, their text on the
  volume. The PVC survives the deletion of the StatefulSet (Kubernetes
  default policy): deleting it is an explicit act.
* **Before any update**: `pg_dump` (migrations do not roll back).

## 6. Harness credentials

* **API keys**: in the `ameesh-harness` Secret, injected into the runner's
  environment and inherited by the harness. They are billed by usage:
  ameesh's hourly cap (`AMEESH_BUDGET_USD_PER_HOUR`, decision 0019) and the
  provider's apply. The harness (hence the agent) can read them.
* **Subscriptions** (interactive login, `credential_mode: subscription`):
  their use on a server, with no human in front, and shared by an autonomous
  agent, **must be checked against each provider's terms** before any
  deployment. This profile does not decide; the `Host` policy
  (`credential_modes`) can simply not allow it.
* No credential is in the image, the canon or the database (spec §3).

## 7. Update

1. `pg_dump` of the database (section 5).
2. New image, new tag (never reused), `newTag` in `kustomization.yaml`,
   `kubectl kustomize` reviewed, `kubectl apply -k`.
3. The StatefulSet replaces the pod: SIGTERM, lease released; the new pod
   applies the migrations (`migrate` init, under advisory lock), clones the
   canon and syncs it, then the runner resumes the agent on the **same
   session** (HOME is kept).
4. `ameesh doctor --probe` (ready probe), `ameesh list`, then rerun
   `role-superviseur.sql`.

## 8. Rollback

* **Image**: put back the old tag and reapply (or `kubectl
  rollout undo statefulset/ameesh-agent`). If the new version applied
  migrations, the old code runs on a newer schema: the probe stays ready and
  reports it (« base plus récente que ce code », "database newer than this
  code"); compatibility is **not** guaranteed. When in doubt: restore the
  `pg_dump` taken before the update (state written since is lost).
* **Take the agent out of service**: `kubectl scale statefulset/ameesh-agent
  --replicas=0` (lease released, harness stopped with its group); or
  `ameesh run stop <agent>` — `canon sync` never lifts this stop.
* **Go back to the local profile**: stop the pod as above, check that the
  lease is released (`ameesh show <agent>`), move the agent's `Placement` to
  the local host by canon PR, `ameesh canon sync` on the local host. The
  sessions and threads left on the volume can be retrieved with `kubectl cp`
  if needed; a harness session is not necessarily portable from one machine
  to another.
* **Remove everything**: `kubectl delete -k deploy/k8s`; the PVC and the
  Secrets remain until they are explicitly deleted.

## 9. Limitations and open questions

* Postgres role **per agent** (or per pod) to isolate the agents' state from
  one another: absent in v1 (all writes go through ameesh's role).
* **Migrations by the `migrate` init, with the runner's role** (provisional
  choice). Risk: this role must have DDL rights on the schema (owner), and
  the harness, which inherits `PGPASSWORD`, therefore has them too — it can
  modify or drop ameesh's tables, triggers and functions, including the
  gate's guards (`ameesh_actions_guard`…). **Planned follow-up**: a
  distinct schema-owner role, used only by a migration Job started by the
  operator before the update; the runner receives a role without DDL (DML on
  its tables), and the `migrate` init disappears in favour of the probe
  (`doctor --probe` refuses a schema that is behind).
* `ameesh-approve` in a cluster (another namespace, another service
  account, exposure via the Nexlink page, decision 0017): outside this
  profile.
* Preparing HOME (harness hooks, work repository): by hand or via a derived
  image; a dedicated init could do it.
* Canon federation across several repositories: not covered by the `canon`
  init.
* No `livenessProbe`: a runner stuck without dying is not detected by
  Kubernetes (the lease, however, expires and the agent is no longer served).
