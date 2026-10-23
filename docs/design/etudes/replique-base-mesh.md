---
type: Study
title: "Database replica and failover: the mesh's last single point (EP8)"
description: "EP8: the mesh database and the WireGuard hub are one VM. Three PostgreSQL 17 nodes on the existing VMs, synchronous streaming replication, etcd + Patroni for a deterministic failover (target under one minute, requirement under five), a multi-host DSN, WAL archiving and PITR; six lots, no new VM, no overage."
status: proposed
tags: [postgres, replication, bascule, disponibilite, etcd, patroni, wireguard, sauvegarde]
generated: { by: "ameesh-dev3/deepseek-flash", at: "2026-10-22T20:45:00Z" }
sources:
  - { resource: "https://github.com/manaty/ameesh/pull/2303", title: "Study — electing a pilot when its host stops (EP1–EP7), PR #2303" }
  - { resource: "../decisions/0016-stockage.md", title: "Decision 0016 (Postgres for v1)" }
  - { resource: "../../BASCULE.md", title: "Cutover to ameesh v1 (database, WireGuard, backups)" }
  - { resource: "../../HOST-HANDOFFS.md", title: "Durable host handoffs (L232a/L232e)" }
  - { resource: "../../EXPLOITATION.md", title: "Operations manual (transient database outage, L72)" }
---

# Database replica and failover: the mesh's last single point

## Summary

- The mesh database and the WireGuard hub are the same VM, `manaty-mesh-1`.
  A loss of that VM stops every agent, even with the pilot election of EP1–EP7:
  there is no database to hold a lease, and no WireGuard path between the VMs.
- Recommendation: three PostgreSQL 17 nodes on the three existing VMs,
  synchronous streaming replication between the two 8 vCPU agent VMs,
  etcd + Patroni for a deterministic failover (target under one minute,
  requirement under five, as in the RTO row), a private
  network so the hub leaves the database path, a multi-host DSN, and WAL
  archiving with point-in-time recovery to object storage.
- Six lots, about 34 agent-hours, no new VM. The recommended design's only new
  spend is the object-storage bucket (small, to be approved if it does not
  exist); a fourth voter is an option with a new monthly cost, not part of the
  recommendation.
- The owner decides the network (private network or WireGuard mesh), the
  primary VM, the bucket, the optional PC copy and the switchover window.

## What happened

The owner's workstation (`pc-smichea`) was off from 2026-10-21 12:09 to
2026-10-22 21:54 (Paris). The VMs stayed up and the database stayed up on
`manaty-mesh-1`; the mesh nevertheless stopped producing, because the
governance roles were placed only on the workstation. The EP1–EP7 study
([PR #2303](https://github.com/manaty/ameesh/pull/2303),
`docs/design/etudes/election-pilote.md`) fixes that placement.

The database is the next single point, and it is coupled to the network:

- `manaty-mesh-1` is both the Postgres server and the WireGuard server. Each
  device's WireGuard configuration has `AllowedIPs = 10.77.0.1/32`, so the
  VMs cannot reach each other; `ip route get 10.77.0.4` leaves through the
  public default route, not through the tunnel. A replica on `ameesh-agents-1`
  would be unreachable from `ameesh-agents-2` while the hub is down.
- The database has no replica (`pg_stat_replication` is empty) and no WAL
  archiving (`archive_mode=off`). The backup path is a daily `pg_dump -Fc`
  on the database host, kept 14 days locally, plus the workstation's
  `poste.sh sauvegarder` when the PC is on; the S3 copy is optional
  (`deploy/serveur/sauvegarder-base.sh` copies only if
  `/etc/ameesh/sauvegarde.env` exists). A disk loss loses up to 24 hours, and
  neither copy is continuous.
- The database host is not sampled: `host_resources` and `host_lifecycle`
  cover the agent hosts, not `manaty-mesh-1`. Its health is seen only when a
  client's connection fails.

The owner's rule (2026-10-22):

> « il ne faut pas qu'un mesh s'arrete de travailler si le pc est eteint, il
> faut qu'un autre pilote soit elu parmis les instances restantes, de
> preference celle qui a le moins de chance de s'arreter »

A mesh must not stop working when a host stops; EP8 is the floor under that
rule, because a pilot cannot be elected without the database that holds its
lease.

## Current state (read-only measurements, 2026-10-22 20:34–20:45Z)

### Hosts

| Host | Role today | Resources | Disk free | WireGuard |
|---|---|---|---|---|
| `manaty-mesh-1` | Postgres primary, WireGuard hub, daily `pg_dump` | 4 vCPU / 8 GB, no swap (DEV1-L, 80 GB local SSD default) | not sampled | `10.77.0.1` |
| `ameesh-agents-1` | 13 agents, 12 runners; database client | 8 vCPU / 31 GB | 24 GB | `10.77.0.3` |
| `ameesh-agents-2` | 14 agents; database client | 8 vCPU / 31 GB | 13 GB (90 %, `host_disk_soon_full` alert) | `10.77.0.4` |
| `pc-smichea` | 21 placements, human; database client | 12 vCPU / 23 GB | 152 GB | `10.77.0.2` |

The three VMs are in the Scaleway `fr-par-1` zone (measured on
`ameesh-agents-1`; the mesh VM Terraform defaults to `fr-par-1`). None has a
private NIC today (`private_ip: null`, `EXTRA_NETWORKS=0` on
`ameesh-agents-1`), so a private network is an infrastructure change, not a
configuration toggle.

### Database

| Fact | Value (2026-10-22 20:35–20:45Z) |
|---|---|
| Version | PostgreSQL 17.11 (Debian 17.11-1.pgdg12+2) |
| Size | 1,492 MB |
| Connections | 133 / 300; 114 idle, 9 active (the rest other or unclassified; the count moves with the clients) |
| Clients | 59 from `10.77.0.2` (PC), 32 from `.3`, 31 from `.4`, 11 local |
| Transactions | 227.6 M commits since 2026-10-09 (about 16.7 M/day, 190/s) |
| WAL | 19.7 GB recorded since 2026-10-09 (about 1.5–2 GB/day); `max_wal_size=1GB` |
| Cache | 88.7 % buffer-cache hit (22.4 B disk reads, 175.9 B hits) |
| Replication | none; `wal_level=replica`, `max_wal_senders=10`, `max_replication_slots=10`, `hot_standby=on` |
| Archiving | `archive_mode=off`, `archive_timeout=0`; no point-in-time recovery |
| Rewind | `wal_log_hints=off`, `data_checksums=off`; `pg_rewind` needs one of them |
| TLS | `ssl=on`, Debian snakeoil certificate, `sslmode=require` (encryption without verification) |
| Backups | daily `pg_dump -Fc`, 14 days local; the S3 copy exists only if `/etc/ameesh/sauvegarde.env` does |
| Migrations | 135 |
| Largest tables | `host_resources` 444 MB, `conversation_log` 391 MB, `agent_mailbox` 246 MB, `housekeeping_log` 177 MB |

The top tables are append-heavy; the measured 9 active sessions and 88.7 %
buffer-cache hit do not point to a CPU-bound workload. A failover is mostly a
connection and reconnection problem, not a throughput problem.

The server is already configured to accept standbys (`wal_level=replica`,
`max_wal_senders=10`, `hot_standby=on`). Enabling `wal_log_hints=on` (for
`pg_rewind`) and `archive_mode=on` (for PITR) requires one restart of the
current server. The current database is the only one, so that restart is a
short outage; L72 already gives the clients a 60 s retry and at least 200 s
of lease margin, so a 30 s restart costs no turn.

### What already works in the client

- **L72** (`docs/EXPLOITATION.md`, "Panne passagère de la base") already
  rides out a transient outage: retry every 2, 4, 8 … seconds up to
  `AMEESH_DB_RETRY_MAX=60`, a lease margin of at least two thirds of
  `AMEESH_LEASE_TTL` (200 s with the 300 s default), and a LISTEN/NOTIFY
  listener that reconnects with the same backoff. A failover under 60 s is
  invisible to a turn in progress.
- **Multi-host DSN.** `dsn.split_password` and `config.mask_dsn` parse a
  three-host DSN. A `psql` test with
  `target_session_attrs=read-write` skipped two hosts without a server and
  connected to `10.77.0.1`; the psycopg path uses the same libpq, so it should
  behave the same, but it must be proven on staging.
- **Migrations** take an advisory lock and run on the primary; a standby
  reached through `target_session_attrs=read-write` is skipped. The schema is
  replicated by streaming replication, so a failover does not lose applied
  migrations.
- **PostgreSQL 17 client tools** are needed on the DB nodes and the restore
  host. `ameesh-agents-1` currently has only the 16 client (`psql 16.15`);
  `pg_basebackup`/`pg_rewind` from 16 cannot drive a 17 server.

## Requirements

| Requirement | Target | Why |
|---|---|---|
| RPO | 0 for the loss of one host | the lease, the epoch, the mailbox and the work journal must not lose a committed transaction |
| RTO | under 5 min, target under 1 min | EP7's five-minute pilot target; the 300 s lease TTL and L72's 200 s margin |
| Split brain | never | two primaries would fork the lease epochs, the mailbox and the work journal |
| PC off | no effect on the database | the owner's rule of 2026-10-22; EP1–EP7 moves the roles |
| Determinism | etcd + Patroni, no agent vote | canon §7: agents judge, code decides |
| Cost | no new VM; object storage only | no overage; the owner approves any new spend |
| Security | no public database, TLS, least privilege | the current design; SQL and rights are sensitive |
| Reversibility | the old database and its dumps stay until the cluster is proven; a switchback exists | canon §2 and §14 |
| Staging | no production; a real drill | canon §14; the mesh is staging |

## Options

| Option | RPO | RTO | Split brain | Added components | Cost | Verdict |
|---|---|---|---|---|---|---|
| Status quo (single database, daily dump) | up to 24 h | 30–60 min restore | no | none | 0 | rejected: the mesh stops |
| Cold standby, manual promotion | minutes | 30–60 min plus a human | possible | none | 0 | rejected: a human decision, and a partition can fork |
| Streaming replication + repmgr/script | 0 with sync | 1–5 min | possible on partition | repmgr | low | rejected: no quorum |
| `pg_auto_failover` | 0 | under 1 min | prevented by the monitor | monitor + keepers | low | fallback: the monitor is a single decision point |
| **Patroni + etcd** | **0** | **under 1 min** | **prevented by the quorum** | etcd + Patroni | low, existing VMs | **recommended** |
| Managed Postgres | provider | provider | provider | external service | monthly | rejected for now: data leaves the mesh, cost and lock-in |
| Multi-primary (BDR) | 0 | 0 | conflicts | BDR | high | rejected: overkill for 1.5 GB |

Patroni + etcd is the smallest design that removes the *decision* point as
well as the data point:

- `pg_auto_failover` is simpler, but its monitor is a single decision point.
  If the monitor dies, the primary keeps serving, but a later primary failure
  has no failover until the monitor is back. That is the same class of stop
  the study is trying to remove.
- `repmgr` has no consensus; a network partition can leave two nodes that
  both believe they should be primary.
- A managed service moves the data outside the mesh and adds a contract and
  a monthly spend; it remains a fallback if the operational burden of etcd
  proves too high, and the owner decides.
- Multi-primary is not justified for a 1.5 GB database with about 190 tx/s.

The EP1–EP7 study deliberately adds no protocol between hosts for the pilot.
EP8 is the exception it names: the database is the single point, and etcd is
the smallest consensus layer that removes it. Nothing here changes the
pilot's lease or election.

## Design

### 1. The network leaves the hub

The database and the WireGuard hub must not share a failure domain.

**Preferred: a Scaleway Private Network.** Attach a private NIC to the three
VMs, give them private addresses, and let Postgres listen on the private
address only (`listen_addresses = 'localhost,<private>'`, `pg_hba` on the
private subnet with `scram-sha-256`). The database nodes talk directly on the
private network; the hub is no longer on the replication path. This requires
the three VMs to be in the same Scaleway project and region — measured on
`ameesh-agents-1` (`fr-par-1`) and implied by the mesh Terraform's
`fr-par-1` default, but to confirm for `manaty-mesh-1` before the lot; a
Private Network is project-scoped. The PC reaches the primary through the hub
(the hub forwards to the private network) or through a direct WireGuard peer;
the PC's access is best-effort, while the VMs' work continues if the hub
stops. This needs the owner/bureau to attach the private NICs and the
security-group rules; it is an infrastructure change.

**Fallback: a partial WireGuard mesh.** Add each database node as a peer of
the other two with a `/32` `AllowedIPs`, open UDP 51820 between the three VMs
in their security groups, and keep the hub for the PC and the other devices.
The clients' current `AllowedIPs = 10.77.0.1/32` must change; this is a real
configuration change on every device, not a toggle. A second WireGuard hub
is not a solution: WireGuard has no automatic peer failover, and two peers
with the same `AllowedIPs` are ambiguous.

The hub remains the WireGuard server for the PC and other devices. Its loss
no longer stops the database pair; the PC loses database access until the hub
returns or a direct peer is configured. That is acceptable for a workstation
that is off half the time, and it is a deliberate choice: the study does not
make the PC a quorum member.

### 2. Three nodes, two of them the primary candidates

| Node | Role | Why |
|---|---|---|
| `ameesh-agents-1` | primary candidate (8 vCPU / 31 GB, 24 GB disk free) | the GP1-S pair is the reliable pair; the database is 1.5 GB and the host has memory headroom |
| `ameesh-agents-2` | primary candidate (8 vCPU / 31 GB, 13 GB disk free) | the second half of the pair; clean its disk before it carries a standby (base backup + local WAL ≈ 4–5 GB) |
| `manaty-mesh-1` | etcd voter + asynchronous standby + WireGuard hub | 4 vCPU / 8 GB without swap is too small to be the primary; it stays a voter and a standby so the cluster keeps a third copy |
| `pc-smichea` | optional delayed DR copy; never a voter | it is off often; a copy there protects against a bad migration, not against a host loss |

The two agent VMs are the primary and the synchronous standby; `manaty-mesh-1`
is a voter and an asynchronous standby with `nofailover: true` and
`nosync: true`: it receives replication and votes in etcd, but it is never
promoted automatically and never enters `synchronous_standby_names`, so no
commit waits for the smallest, hub-loaded host. If both agent VMs are lost,
the surviving hub has no etcd quorum (1/3) and does not promote; the mesh
stops. The hub is asynchronous and may miss the last commits; a restore from
the WAL archive (section 6) stops at the last segment actually archived, so
those commits can be lost. That is a double failure, not the single failure
this study removes.

The primary starts on `ameesh-agents-1` and follows the EP2 candidate score
(availability, readiness, capacity) on a planned switchover. The current
primary on the 4 vCPU host moves off it: it is the smallest host and it has
no swap.

### 3. Replication

- PostgreSQL 17 streaming replication from the primary to the two standbys,
  through `pg_basebackup -R -X stream`, replication slots, and
  `hot_standby=on`.
- `synchronous_commit=on` and `synchronous_standby_names` managed by Patroni.
  Recommendation: `synchronous_mode: true`, `synchronous_node_count: 1`
  (the primary and the other agent VM are the synchronous pair; the hub stays
  asynchronous), `synchronous_mode_strict: false`, and
  `maximum_lag_on_failover: 1MB`. This gives RPO 0 for the loss of any one
  host: if the primary is lost, the synchronous agent is promoted with every
  committed transaction; if the synchronous standby is lost, the primary keeps
  the role and the other two voters keep the etcd quorum.
  `synchronous_mode_strict: false` is an availability choice: if the
  synchronous standby is unavailable while the etcd quorum remains, the
  primary keeps serving with a degraded RPO rather than blocking writes; a
  double host loss loses the quorum and the cluster stops rather than forks.
  `manaty-mesh-1` carries the Patroni tag `nosync: true`: it is never a
  synchronous standby, so the cluster never pays the hub's latency on a
  commit, and it never becomes the only synchronous standby when an agent VM
  is down (the primary then runs without a synchronous standby, as
  `synchronous_mode_strict: false` allows, and a further loss is bounded by
  the last archived WAL segment).
  A second synchronous standby would add no availability anyway, because a
  double host loss also loses the etcd quorum. The lot verifies that Patroni
  reports the other agent VM as `sync_state=sync` and the hub as `async` in
  every state, including with one agent VM stopped.
- `wal_log_hints=on` is required for `pg_rewind` (the current server has it
  off; enabling it is part of the restart in EP8.1). Without it, a returning
  old primary is re-seeded with `pg_basebackup`, which is slower but correct.
- `wal_keep_size` plus slots must cover the longest expected standby outage;
  a slot that is never consumed fills the primary's disk, so the alerting on
  `pg_replication_slots` is part of the design, not an option.
- Archiving is enabled (`archive_mode=on`) for PITR; the archive command
  belongs to `pgBackRest` (section 6).

### 4. Failover: etcd + Patroni

- Three etcd 3.5.x voters (one per VM), TLS on the peer and client ports,
  client-certificate authentication, no public listener. A quorum of two is
  enough; the two agent VMs are the usual majority, the hub is the third
  vote.
- Three Patroni 4.x instances, one per VM, managing the local PostgreSQL.
  Settings: `ttl: 30`, `loop_wait: 10`, `retry_timeout: 10`,
  `maximum_lag_on_failover: 1MB`, `synchronous_mode: true`,
  `synchronous_node_count: 1`, `synchronous_mode_strict: false`,
  `failsafe_mode: false` (a DCS outage pauses writes instead of risking a
  fork), `postgresql.use_pg_rewind: true`. Priorities:
  `ameesh-agents-1: 100`, `ameesh-agents-2: 90`, `manaty-mesh-1: 0` with
  `nofailover: true` and `nosync: true`.
- Fencing is the etcd leader lease plus the Patroni leader lock. A primary
  that loses the DCS demotes when its lease expires, before the new leader is
  elected; a partitioned old primary therefore cannot accept writes while
  the new one is promoted. A hardware watchdog (`/dev/watchdog`, if the VMs
  expose one) adds a fence for a hung primary; it is tested, not assumed.
- After a failover, the old primary rejoins with `pg_rewind` and restarts as
  a standby. A failed `pg_rewind` is not hidden: the node is re-seeded with
  `pg_basebackup` and the operation is recorded.
- Timings: about 20–30 s to detect and elect, about 5 s to promote, about
  5–15 s for clients to reconnect; well under the one-minute target, under
  EP7's five minutes, and inside L72's 200 s lease margin. No turn is lost
  for a normal failover.
- No agent decides a promotion. `patronictl` and `ameesh db status` report;
  the failover is code.

### 5. Clients find the primary

The client DSN becomes a three-host list:

```
postgresql://ameesh@<private-1>,<private-2>,<private-3>:5432/ameesh
  ?sslmode=require&target_session_attrs=read-write&connect_timeout=10
  &keepalives=1&keepalives_idle=30&keepalives_interval=10
  &keepalives_count=3&tcp_user_timeout=60000
```

- libpq tries each host and accepts only a read-write server, so a demoted
  primary or a standby is skipped. The existing psycopg driver passes the DSN
  to libpq unchanged; the LISTEN/NOTIFY listener reconnects through the same
  DSN after its `down` event.
- A NOTIFY is not queued for a listener that is not connected: those sent
  while the listener is down, and those of transactions the old primary had
  not replicated, are never delivered. After every reconnection the listener
  therefore runs a full sweep before waiting again: it wakes every worker so
  each one re-reads its mail, its leases and the budget from the database,
  as the train service's `wake()` already does for its own channel. Today the
  runner relies on its `poll` heartbeat for mail, leases and budget; EP8.4
  makes the sweep explicit and its failover test proves it.
- Every client host needs the same `ameesh` role password for the three
  addresses (a `.pgpass` entry per host, 0600, outside the repository).
  The role and its password hash are replicated; the secret distribution is
  the operational part, not a new secret per node.
- `ameesh migrate` connects to the primary. A migration interrupted by a
  failover aborts as a transaction; the DDL is transactional and the standby
  never sees a partial migration. The migration is retried on the new
  primary.
- The about 133 current connections (about 60 from the PC) are under the 300
  limit; a failover makes them all reconnect through the backoff. The lots
  test the storm with the max-connection headroom. PgBouncer is not active
  today; if the pilot is adopted, its upstream DSN must be the multi-host one,
  and its local socket remains a per-host convenience, not a new single point.
- `ameesh-approve`, `ameesh-notify`, `sauvegarder-base.sh`, `pg_dump` and
  `psql` all go through the same DSN. The approval service is on the PC and
  is outside this study, but its DSN must be updated with the others.

### 6. Backups and point-in-time recovery

- `pgBackRest` (or WAL-G) with the existing Scaleway Object Storage bucket:
  a weekly full backup, daily differentials, continuous WAL archiving,
  14-day retention, and a documented restore command. The backup is taken
  from a standby, not the primary, when possible. Put the bucket in another
  region if the cost is acceptable; otherwise the PC copy is the only
  off-region copy.
- The daily `pg_dump -Fc` stays as a second, independent format and as the
  human-readable restore path. Its S3 copy becomes mandatory and monitored;
  today it is optional (`/etc/ameesh/sauvegarde.env`), which means the only
  backup may be on the very VM that can be lost.
- A restore drill on `ameesh-tests` restores to a chosen point in time,
  runs `ameesh doctor` and a schema check, and records the measured RTO.
  A backup that has not been restored is not a backup.
- The PC copy is a weekly `pg_basebackup` plus WAL replay, delayed if
  desired, and is never a quorum member. It is optional because the PC's
  disk was recovering from the 2026-10-11 incident.

### 7. Operations and observability

- The `deploy/serveur/` scripts gain an idempotent DB role:
  `installer-serveur.sh` (or a `deploy/db/` sibling) installs PostgreSQL 17,
  etcd, Patroni and pgBackRest, writes the TLS material with 0600
  permissions, and never puts a secret in the repository. The run is
  root-owned, as today; agents do not get root on the database nodes.
- `ameesh db status` reports the primary, the standbys, the replay lag, the
  synchronous state, the etcd quorum and the last archived WAL;
  `ameesh doctor` gains a `db` line. The minimum viable version may be a
  small `deploy/db/etat.sh` plus a `patronictl list`; the command is a
  convenience, not the safety mechanism.
- Alerts: `db_no_primary`, `db_failover`, `db_replication_lag`,
  `db_archive_stale`, `db_etcd_no_quorum`, `db_disk_low`. They must reach
  the owner with the PC off; the `veille` agent on a VM and the notify route
  are the path to test. The `host_resources` sampler must include the
  database host, otherwise its disk and memory are invisible.
- Runbooks: planned switchover, node replacement, etcd member replacement,
  `pg_rewind` failure, replica re-seed, disk full, suspected split brain,
  restore, and the PostgreSQL/etcd/Patroni minor upgrade. They live in
  `docs/BASCULE.md` and `docs/EXPLOITATION.md`, not in a new product journal.
- A PostgreSQL major upgrade is a separate lot: `pg_upgrade` on the primary,
  then re-seed the standbys; it is not part of this study.

### 8. Security

- Roles: `ameesh` (clients, no superuser), `replicator` (`REPLICATION`,
  login, password), `rewind` or `postgres` over a local socket for
  `pg_rewind`, and the local `postgres` superuser for Patroni. No new
  superuser reachable from the network.
- `pg_hba`: `hostssl` only, `scram-sha-256`, restricted to the private
  subnet and the PC's address; local peer for `postgres`; no `trust`.
- TLS: the current `sslmode=require` with the Debian snakeoil certificate is
  encryption without verification; the private network or WireGuard is the
  authentication today. A mesh CA with `sslmode=verify-full` is a follow-up,
  not a prerequisite.
- etcd: TLS for peers and clients, client-certificate authentication,
  `--listen-client-urls` on the private address only. Patroni's REST API:
  TLS and authentication, private address only. No new public port.
- The SQL (roles, `pg_hba`, slots) and the infrastructure (security groups,
  private NICs, etcd) are sensitive: two families review them, and the merge
  gate of the canon applies.

## Guarantees and limits

| Failure | What happens | RPO | RTO |
|---|---|---|---|
| A primary agent VM stops | the other agent is promoted | 0 | under 60 s |
| A standby agent VM stops | the primary continues; Patroni reconfigures the sync set | 0 | no outage |
| `manaty-mesh-1` (hub) stops | the two agents keep working on the private network; the PC loses database access; etcd keeps 2/3 | 0 | 0 for the VMs; the PC when the hub returns |
| The PC stops | no effect on the database or the VMs | 0 | 0 |
| The primary and one other node stop | the surviving etcd is 1/3: no quorum, the mesh stops; the survivor may be the asynchronous (`nosync`) hub and miss the last commits, which a restore recovers only up to the last archived WAL segment; it cannot serve alone | 0 on the sync survivor; possibly the last commits on the asynchronous hub | repair or restore; a fourth voter would be needed to survive two |
| The whole `fr-par-1` zone | restore from the off-region S3 archive, or from the PC copy if it is current, on a new VM | up to the last archived WAL | hours |
| A logical error (bad migration, bad delete) | point-in-time recovery to before the error | 0 at the restore point | 30–60 min |

The study removes one-host failures. It does not make the mesh survive two
simultaneous host failures, a region loss without a restore, or the external
dependencies: GitHub, the canon repository and the human approval path. The
`pc-smichea` placement problem is EP1–EP7's; the coding-agent moves stay
L232e's.

## Lots

| Ref | Lot | Class | Estimate | Depends on | Acceptance |
|---|---|---|---|---|---|
| EP8.1 | Off-host WAL archiving and PITR (`pgBackRest` to S3); keep the daily dump; enable `archive_mode`/`wal_log_hints` with one short restart; restore drill | sensitive (data/ops) | 4 h | S3 bucket | a restore to a chosen point on `ameesh-tests` passes `ameesh doctor`; the daily dump still works |
| EP8.2 | Private network among the three VMs (preferred) or the WireGuard partial mesh; database on the private addresses; `pg_hba`; the PC path; test with the hub down | sensitive (infra/rights) | 6 h | owner/bureau infrastructure | each DB node reaches the others with the hub stopped; no public database; the PC path is documented |
| EP8.3 | Three-node streaming replication; PostgreSQL 17 client tools; `replicator`/`rewind` roles; slots; synchronous configuration; manual switchover and `pg_rewind` | sensitive (SQL/rights) | 6 h | EP8.1, EP8.2 | `pg_stat_replication` shows two standbys, the synchronous ones at zero lag; a manual promotion works; the old primary rejoins |
| EP8.4 | etcd + Patroni: three voters, TLS, automatic failover, `pg_rewind`, priorities/`nofailover`/`nosync`, `patronictl`, alerts, the multi-host DSN and client tests | sensitive (failover/rights) | 8 h | EP8.3 | a killed primary is replaced in under 1 min (the target) with no data loss; a partition shows no two primaries; the clients reconnect; after the failover the listener sweeps mail, leases and budget (a message sent during the failover is delivered without waiting for the poll) |
| EP8.5 | `ameesh db status`/`doctor`, the `db_*` alerts, the multi-host DSN documentation and `.pgpass`, the PgBouncer upstream, the `sauvegarder-base.sh` update | sensitive (`deploy/serveur`, ops): two families | 4 h | EP8.4 | the status shows the primary and the lag; the alerts fire with the PC off; the PgBouncer path is tested if adopted. A new `ameesh` command makes this a minor version; a shell-only option stays a patch |
| EP8.6 | Live switchover to the cluster, the canon host cards, the runbooks, the drill (primary kill, network cut, PC off, two-node loss) and its report | sensitive (ops), release proof | 6 h | EP8.4, EP8.5 | the mesh runs on the cluster; a primary loss gives a new primary in under 1 min (target; 5 min is the requirement) with RPO 0; no split brain; the old primary rejoins; the report is on the lot |

About 34 agent-hours in total. EP8.1 and EP8.2 can start in parallel; the
rest follow. The estimates are agent-hours; review, CI and the packet wait
are excluded. No lot is a production change: the mesh is staging under canon
§14, and the live switchover is a staging operation by the pilot or the
bureau, with the same rollback discipline as any other change.

## Owner decisions

The study recommends a default for each; EP8.1 can start once the S3 bucket
exists and the restart window (decision 8) is agreed.

1. **Network**: a Scaleway Private Network (recommended) or the WireGuard
   partial mesh. The private network is cleaner: no new keys, no public port,
   no hub on the database path. It requires the three VMs to share a project
   and a region, and a private NIC on each; that is an infrastructure change
   for the owner/bureau. If the project cannot be shared, the WireGuard mesh
   is the fallback.
2. **Primary**: `ameesh-agents-1` at first, then the EP2 candidate score.
   `ameesh-agents-2` has more memory free but only 13 GB of disk (90 % used);
   clean it before it carries a standby. `manaty-mesh-1` stays
   `nofailover`.
3. **Synchronous set**: not an open choice. The count is 1 (the two agent
   VMs); the hub is tagged `nosync` and stays asynchronous, because a second
   synchronous standby would add its latency to every commit and no
   availability. The double-failure copy is the WAL archive, not the hub;
   it is complete only up to the last archived segment.
4. **A fourth voter**: not for one-host tolerance. A small witness VM would
   let the mesh survive two host failures at a new monthly cost; the owner
   decides if that is worth it.
5. **Managed Postgres**: not now. Revisit only if the etcd/Patroni operation
   proves too heavy for the team; it moves the data outside the mesh.
6. **Object storage**: use the existing bucket if the S3 copy is configured;
   otherwise create one, with a 14-day retention, preferably in another region
   than `fr-par-1`. The owner approves the small spend.
7. **PC copy**: a weekly `pg_basebackup` on `pc-smichea` if its disk is
   healthy; never a voter.
8. **Switchover window**: a 30–60 s database restart for EP8.1 and a planned
   switchover with no production. The pilot or bureau schedules it; the owner
   is informed.

## To validate in the lots

- The inter-node RTT and the synchronous commit latency between the two
  agent VMs; that the `nosync` hub never appears in
  `synchronous_standby_names`, including with one agent VM stopped.
- That the three VMs share a Scaleway project and region, or the fallback to
  the WireGuard mesh; the private NICs and the security groups.
- The free disk and the WAL budget on `manaty-mesh-1`; the 13 GB on
  `ameesh-agents-2`; the 14-day object-storage retention against the
  1.5–2 GB/day WAL; the bucket's region against a region loss.
- The memory reservation on the two agent VMs: the database processes are
  outside the agents' 6 GB slice, and the host's memory guard must keep room
  for them without pausing the agents.
- The S3 copy of the current daily dump; restore it before changing anything.
- The workstation `poste.sh sauvegarder`: its `pg_dump` must be 17 or newer
  for the 17 server, and its copy is only as good as the PC's uptime.
- The psycopg multi-host reconnect, the LISTEN/NOTIFY listener after a
  failover, and a migration interrupted by a failover.
- The ~133-connection reconnect storm and the `max_connections` headroom.
- `pg_rewind` with `wal_log_hints=on`; the old primary's rejoin; the
  re-seed path when `pg_rewind` fails.
- The etcd/Patroni versions and the TLS material; the `patronictl` commands;
  the watchdog device, if the VMs expose one.
- The alert path with the PC off; the `veille` agent on a VM.
- The `host_resources` sampler for the database host.
- The PgBouncer upstream, if the pilot is adopted.
- The PostgreSQL 17 client tools on the DB nodes and the restore host.
- The `ameesh-approve` DSN and reconnect, since the approval service uses the
  same database from the PC.
- The canon host cards and the federation, if they list the database service.

## Not in scope

- The pilot election and its lease/epoch (EP1–EP7). The database failover is
  transparent to the lease: the epoch is replicated, and the clients reconnect
  to the new primary.
- The migration of coding agents (L232e) and their source proof.
- The canon, GitHub and the human approval path; they are external and the
  study does not remove them.
- A multi-region disaster recovery. The PC copy is a start; a second region is
  a separate decision.
- Multi-primary, sharding, or a rewrite of the storage layer (SQLite, v2).
- The Nexlink database (Supabase), which has its own failover; the principles
  transfer, the lots are for the ameesh mesh.

## References

- PostgreSQL 17: [streaming replication](https://www.postgresql.org/docs/17/warm-standby.html),
  [synchronous commit](https://www.postgresql.org/docs/17/runtime-config-wal.html#GUC-SYNCHRONOUS-COMMIT),
  [pg_basebackup](https://www.postgresql.org/docs/17/app-pgbasebackup.html),
  [pg_rewind](https://www.postgresql.org/docs/17/app-pgrewind.html).
- [Patroni](https://patroni.readthedocs.io/), [etcd](https://etcd.io/docs/),
  [pgBackRest](https://pgbackrest.org/).
- Scaleway [Private Networks](https://www.scaleway.com/en/docs/network/private-networks/).
- `docs/BASCULE.md`, `docs/EXPLOITATION.md` (L72), `docs/HOST-HANDOFFS.md`,
  `docs/design/decisions/0016-stockage.md`, `deploy/serveur/`.
- The EP1–EP7 study: [PR #2303](https://github.com/manaty/ameesh/pull/2303).
