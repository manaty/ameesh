# Cluster profile

The **cluster** hosting profile runs **one** persistent ameesh agent in a
Kubernetes pod. It is a minimal profile: the runner and its harness, nothing
else. Neither the gate with human receipts nor ameesh-approve is exposed; the
agent is declared in the canon with the capabilities `read` and `propose` only.

Files in the repository:

| File | Role |
|---|---|
| `Dockerfile`, `.dockerignore` | the runner image, without secrets, without a harness by default |
| `deploy/k8s/` | example manifests (kustomize): StatefulSet, ConfigMap, Service without ports, NetworkPolicy, service account |
| `deploy/k8s/secrets.exemple.yaml` | the shape of the expected Secrets, never filled in, never applied as is |
| `deploy/sql/role-superviseur.sql` | read-only Postgres role for an external supervisor (state only) |
| `deploy/sql/role-superviseur-contenus.sql` | additional role that can read message bodies, prompts and notes, granted explicitly |

## Architecture

```
                 canon repository (git)                    durable Postgres
                 Host = pod name,                          (outside the pod)
                 Agent, Placement                                 ▲
                        │ read-only deploy key                    │ AMEESH_DSN + PGPASSWORD
  ┌─ pod ameesh-agent-0 ┼─────────────────────────────────────────┼─────────┐
  │  init migrate   ameesh migrate ──────────────────────────────►│         │
  │  init canon     git clone ; ameesh canon sync --fetch ───────►│         │
  │  canon-fetch ─► canon/ (periodic git fetch), mounted READ-ONLY in runner │
  │  runner         agent-runner ── periodic canon sync ─────────►│         │
  │   (tini, PID 1)   └─ harness (claude | codex | dsh)                     │
  │                  HOME = persistent volume: sessions, state, threads, cwd│
  └─────────────────────────────────────────────────────────────────────────┘
     no inbound network (Service without ports, NetworkPolicy);
     outbound: database, git, model provider API
```

- **Identity.** The pod name (`ameesh-agent-0`) is the ameesh host
  (`AMEESH_HOST`, via the downward API). The canon declares a `Host` of that
  name and the agent's `Placement` on it; without them the agent is not
  claimable. Always one replica: two replicas would be two hosts.
- **Canon.** Cloned by an init container with a **read-only** deploy key, then
  fetched periodically by a sidecar. The clone is mounted **read-only** in the
  runner container; the key is never mounted there. `canon_ref: origin/main`
  in the configuration fixes the trusted canonical branch.
- **State.** The database holds leases, mailbox, registry and actions. The
  persistent `home` volume holds harness sessions, ameesh's local state
  (including the readable threads) and the working directories.
- **Shutdown.** tini forwards `SIGTERM` to the runner, which stops the harness
  group, joins its turns and releases the lease.
- **Probe.** `ameesh doctor --probe` as `readinessProbe`: database reachable,
  schema present, migrations up to date; read-only. No `livenessProbe`.

## What is guaranteed, and what is not

| Guaranteed in this profile | Not guaranteed |
|---|---|
| **one agent per pod**: its file system, processes and sessions are isolated from other agents, which the local profile (shared Unix user) does not offer | isolation **in the database**: the runner and its harness share ameesh's Postgres role; the agent can read and modify the state of other agents of the same schema |
| the canon is **read-only** for the agent; the deploy key is not mounted in its container | the agent can write to the database with ameesh's role; monotonicity and the claim predicate remain the only safeguards |
| read-only root file system, no Linux capabilities, non-root user, no Kubernetes API token | outbound network open in the example; no network sandbox or MCP proxy |
| no inbound network: the gate and ameesh-approve are **not exposed**; an `irreversible` or `costly` action stays blocked (`[receipt_required]`), the safe behaviour | no human approval is possible from this profile: ameesh-approve must be deployed **out of the agents' reach**, which this profile does not provide |
| an agent without a responsible human, misplaced, or whose canon is invalid is not claimed | the example does not check that the agent's capabilities are limited to `read` and `propose`: the reviewed `Agent` card says so |

## Setting it up (operator's acts)

None of this is done by an agent or by the repository: creating a registry, a
database, Secrets and applying manifests are operator's acts (and any spending
is a `costly` action).

1. **Image.** `docker build -t <registry>/ameesh-runner:<version> .`, with
   `--build-arg HARNESS_NPM=<package@version>` for a harness distributed by npm,
   or a derived image for another one. Push it to **your** registry and set it
   in `images:` of `kustomization.yaml`.
2. **Database.** A durable Postgres (managed service, or a dedicated instance
   with volume and backups). A role for ameesh that owns its schema. DSN
   **without password** in `config.json`; the password in the `ameesh-db`
   Secret.
3. **Canon.** `Host` (name = pod name), `Agent` (capabilities `read`,
   `propose`) and `Placement` (`cwd` under `/home/ameesh`) cards, merged by
   pull request; a read-only deploy key, `known_hosts` checked out of band.
4. **Secrets.** The three Secrets of `secrets.exemple.yaml`, created outside any
   repository.
5. **Render, review, apply.** `kubectl kustomize deploy/k8s`, then
   `kubectl apply -k deploy/k8s`.
6. **Harness.** On first start the home is empty: set up the harness hooks
   (`agent-mail hook <harness>`) and the working repository once, by
   `kubectl exec` or a derived image.
7. **Check.** `kubectl logs ameesh-agent-0 -c runner` (lease acquired),
   `kubectl exec ameesh-agent-0 -c runner -- ameesh canon check`,
   `ameesh list`, `ameesh doctor --notify-test`.

## External read-only supervisor

A supervisor, possibly from another organisation, sees the **state** of the
mesh, not the **work** itself, by default:

- `ameesh_superviseur`: registry and overview view, mailbox metadata (sender,
  recipient, kind, status, dates), lots (title, state, assignee), thread index
  without excerpt, actions and attempts without arguments or notes, costs,
  canon state, migrations;
- `ameesh_superviseur_contenus`, granted **explicitly in addition**: message
  bodies, prompts, thread excerpts, action arguments and notes, lot
  descriptions and notes.

Neither role can write, nor read signatures, receipts, nonces, challenges,
public keys or authenticator ids. Each script runs in its own transaction,
audits the role's **effective** privileges (including through `PUBLIC`,
inherited and predefined roles, and default ACLs) and **refuses**, rolling
everything back, if the contract is not met. A supervisor role is member of no
other role. Re-run both scripts after every ameesh upgrade.

```bash
export PGOPTIONS='-c search_path=public'
psql "<admin DSN>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
psql "<admin DSN>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur-contenus.sql
```

## Credentials of the harnesses

- **API keys**: in the `ameesh-harness` Secret, inherited by the harness; the
  hourly cap of ameesh and the provider's own limits apply. The harness (so the
  agent) can read them.
- **Subscriptions**: using them on a server, without a human in front, shared
  by an autonomous agent, **must be checked against each provider's terms**
  before any deployment. The `Host` policy (`credential_modes`) can simply not
  admit them.

## Backups, upgrades, rollback

- **Database**: the managed service's backups, or a daily `pg_dump` copied
  **outside the cluster**, with a restore drill (`ameesh doctor --probe` on the
  restored copy). Take a `pg_dump` before every upgrade: migrations do not roll
  back.
- **Volume**: volume snapshots or periodic copies (sessions and threads live
  there).
- **Upgrade**: new image tag (never reused), `kubectl apply -k`; the new pod
  applies migrations, clones and syncs the canon, and resumes the agent on the
  **same session**.
- **Out of service**: `kubectl scale statefulset/ameesh-agent --replicas=0`, or
  `ameesh run stop <agent>`.

## Known limits

- No Postgres role per agent yet: all writes go through ameesh's role.
- Migrations run from the runner's init container with the runner's role
  (provisional): that role has DDL rights on the schema, and so does the
  harness. Planned: a distinct schema-owner role used only by an
  operator-run migration job.
- ameesh-approve in a cluster is outside this profile.
- Federations of several canon repositories are not covered by the example
  init container.
