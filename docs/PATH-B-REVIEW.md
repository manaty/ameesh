# Path B: release review and test-machine trial

Lot 100 assembles L107–L114 and the L113/L115 trial for the mediated
executor. The review target is PR #216 and issue #62. This document describes
execution on the test machine; it does not record a new trial result.

## Content

The server exposes `/api/exec/v1` with a closed operation table, scoped
access, live-lease fencing, transactional idempotency and audit records.
`backend=mediated` uses the remote storage client without a database DSN or
forge credentials on the device. Device enrollment uses a single-use code,
a P-256 key, short-lived access tokens and lease-bound session tokens.
Revocation invalidates tokens and releases leases.

The host gate reports `available`, `draining` or `stopped`. It bounds
concurrency, stops new turns during withdrawal, acknowledges release of
leases, and treats unreadable state as stopped on a mediated host. SIGTERM
starts a bounded withdrawal; a second signal stops immediately.

The server's model relay holds the provider credential and records measured
usage. Device cost reports remain available for diagnostics but do not count
in spending caps or interval-based balance reconciliation. Work repositories
travel through the server as archives and Git bundles. Commits are pushed to
the agent's branch by the server; the device's working directory is wiped at
the end of its lease. The container entry point and image signing helper are
included. Building, signing and deploying a production image are separate
operations.

An interrupted model stream without final usage is charged at its requested
output-token cap, retaining any higher observed count. Git publication is
revalidated after preparation under executor and agent row locks, held until
the push and receipt are saved. Lease expiry uses the database clock in a
second statement after locking the agent row, so lock contention cannot
reuse a pre-lock liveness result. The replay window keeps the last 32 receipts
and at most 64 base/head mappings per lease, even for multi-commit bundles.
Within that window, receipts survive response loss and route restarts and
uploads can append from an unacknowledged base without duplicating a prefix.
An evicted base returns 409; no intermediate per-commit mappings accumulate.

The merge retains main's ordinary database behavior, including assignment
from mail, undeliverable-mail checks, harness-specific hooks, session
validation, housekeeping, resource pressure, work estimates and CI tracking.
For mediated sessions, undeliverable-mail checks use the reduced directory.
`--lot` remains a thread label; `--new-lot` is rejected before mail is sent
because contract 1.1 does not permit creating or assigning work. Other new
operations from main remain explicitly refused by the remote contract.
The 2026-10-12 merge adds main's three optional cost metadata parameters to
the mediated insert (null defaults) while keeping `source=device` forced.
Host version polling and boot orphan review stay on classic hosts/server.
The final main merge retains the optional `held=false` parameter of
`work.move` and its open-decision-request check. For a remote transition to
`waiting_human`, the server forces `held=true` even when the session omits it
or sends false; the storage UPDATE atomically requires and locks an open
request. Decision-source/prose reads,
waiting-episode reads and host-history reads stay refused remotely; the
333-operation interface is fully classified without increasing the 62
admitted operations. Credential health operations from L226b also stay
server-only: devices skip local provider health guards and do not report
relay errors as global provider credential failures.

## Migrations

| Migration | Effect |
|---|---|
| `0062_mediated_executor` | Adds `exec_idempotency`, `exec_host_availability`, `exec_audit` and indexes. |
| `0063_executor_identity` | Adds invitations, executors, access/session token hashes, assertion replay prevention and executor events. |
| `0064_model_relay` | Adds `source`, `executor`, `lease_owner`, `lease_epoch` to `turn_costs`; constrains sources to `harness`, `relay`, `device`. Existing rows default to `harness`. |

These migrations contain no table or column removal, rename, truncation or
rewrite of existing business data. Foreign-key cascades apply only to the
new executor token and assertion tables. The supervisor role gains selected
state columns while token/code hashes, replayable responses, keys and device
attestations remain excluded. Number 0064 is reserved for lot 100;
0058–0061, 0065 and 0066 belong to other lots.

Apply migrations before upgrading processes, using the existing migration
workflow. Older ordinary executors can continue on the migrated schema.
Rollback leaves the additive schema in place. Older spending code counts
`device` rows, so a rollback after enrolling devices requires accounting for
that difference and stopping the mediated path first.

## Compatibility verdict: recommend 1.10.0

No released command or option is removed or renamed. `ameesh serve
--exec-only`, `ameesh host` and `ameesh device`, the mediated backend and host
gate settings are additions. Ordinary executors without a configured gate
retain their existing path. Optional cost provenance parameters preserve
existing calls; the migration's `harness` default preserves existing rows.
Canonical JSON keeps its prior default behavior for signatures and receipts;
floating-point support is opt-in for executor request fingerprints.

The existing released executor protocol is unchanged. The new mediated API
ships as contract **1.1.0**, under `/api/exec/v1`; its operation table is not
expanded to silently expose main's newer operations. The 1.0-to-1.1 assembly
corrections concern this previously unreleased mediated path: lease-state
keys, wire notifications, directory fields, host limits, schema-valid golden
fixtures, request fingerprints, session mail/thread access and the additional
assigned-work read. They do not require an existing ordinary executor to
change its contract. Future incompatible changes to the mediated wire API
must use a new protocol version.

Consequently this is an additive minor release, **1.10.0**, rather than
2.0.0. The package version remains 1.9.0 in this branch; release management
owns the version change, merge and deployment.

## Owner deployment plan after merge

The owner deploys this path separately from the preceding ordinary-runner
fix. Record the merged commit, previous workstation and server commits,
database backup, image digest and signed release manifest before starting.
Release management prepares the additive minor release (1.10.0 proposed
above), its version commit and release notes on main. Use that exact commit
for both installations; publish its immutable annotated `v<version>` tag
with `deploy/mise-a-jour/poste.sh publier <commit>` only after verification.
Do not tag the PR head or move an existing tag.

From the reviewed release checkout, the operator uses the existing steps:

1. `deploy/mise-a-jour/poste.sh verifier`, then `sauvegarder`.
2. `deploy/mise-a-jour/poste.sh migrer <commit>` before either installation;
   verify migrations 0062, 0063 and 0064, plus all migrations of that commit.
3. `deploy/mise-a-jour/poste.sh installer <commit>` and `role`, then
   `deploy/mise-a-jour/vm.sh verifier` and `installer <commit>`.
   Review/apply the executor role separately using `role-executor.sh` if
   used by this server; do not grant executor tables to device identities.
4. Run the ordinary restart/control steps on workstation and server.
   Configure the mediated API under the server operator's service, with
   trusted HTTPS, server-side provider/forge credentials and enrollment
   authentication. `vm.sh redemarrer` manages ordinary runner units; it
   does not install an API service or configure TLS.
5. Verify ordinary agents still claim and finish turns, the supervisor
   excludes secret/hash columns, and `/api/exec/v1` rejects unauthenticated
   requests. Run the seven-stage isolated trial below before enrolling
   production devices. Keep its exact commit and evidence directory.
6. Build and sign the executor image using the release procedure in
   `IMAGE-EXECUTEUR.md`; verify its digest/signature before use. The owner
   enrolls the test mesh's host and checks a relay turn, authoritative costs,
   gate drainage/resumption and revocation. Publish the tag/release after
   these checks and record the deployed server/image revisions.

On failed verification, stop admission on the mediated API, drain devices
and revoke enrolled executors while the new server still supports that
operation. Stop its API service and containers before invoking
`deploy/mise-a-jour/vm.sh retour` and `deploy/mise-a-jour/poste.sh retour`.
Retain the additive migrations and audit records. If device cost rows have
already been written, reconcile spending before reopening ordinary turns:
older code counts those diagnostic rows. Check the restored commits, normal
turns and `doctor`; record the failure and retained evidence. Never delete
the database or reuse an enrollment code as a rollback shortcut.

The 12 October integration retains main's extracted `ameesh.host_gate` for
ordinary runners, including guard heartbeat validation, lending defaults,
shared acknowledgments/slots and freeze accounting. The mediated controller
keeps its remote availability sink, socket support and session-token source;
it implements synchronous refresh before claim and counts the foreground
worker through lease release even though its Thread is never started.
Withdrawal deadlines also stop the remaining process group after a safe
point, so a child retaining stdout cannot keep the turn and lease open.
The common `host_gate_drain=0` configuration means the normal 90 s
fallback for mediated devices. Mail recipient locking remains server-only.

## Validation of the branch

Run only targeted offline tests on the owner's workstation. CI runs the full
psycopg suite, the psql driver selection (including the executor server,
enrollment, relay and host-gate database modules), and the platform checks.
The PR description records the final head and exact CI result. An earlier
assembly trial is reported in `EXECUTEUR-MEDIEE.md`; it is historical evidence,
not validation of the final merge head.

## Trial on 78.232.55.189 (instructions only)

An operator should use an isolated checkout of the PR's final head on the
test machine. Do not run this procedure on pc-smichea. The test needs root
for bind-mount ownership (container UID/GID 10001), Docker with its default
bridge gateway `172.17.0.1`, Python 3, Git, curl and OpenSSL. Docker must be
able to pull/build images and fetch the pinned dsh package. No real provider
or forge credential is required, and model requests go to the local fake
provider: there is no paid inference.

Before starting, verify that ports 55620 (Postgres), 18470 (TLS API), and
18471 (fake provider) are free. The script owns the fixed container names
`ameesh-pg-essai-vb`, `essai-vb-exec`, and volumes
`ameesh-pg-data-essai-vb`, `essai-vb-vol`; ensure they are not used by another
trial. The script removes those resources at startup and, normally, on exit.
Use a dedicated trial host with no unrelated dangling images: the existing
script also prunes dangling Docker images after its build.

From the repository root, prepare the Python environment and then run the
trial as root with that environment's interpreter:

```sh
python3 -m venv .venv-trial
.venv-trial/bin/python -m pip install -e '.[crypto]'
sudo .venv-trial/bin/python scripts/path-b-trial/trial.py \
  --dsh npm --dossier /srv/ameesh/lot100-trial
```

`--dsh npm` uses the real pinned dsh harness against the fake local provider.
The `--dsh faux` alternative is useful for harness plumbing but cannot prove
relay-measured spending because that harness does not call the provider.
`--garder` retains containers and volumes for diagnosis; the operator must
then clean up the named trial resources. Do not replace the fake provider
with a paid endpoint for this trial.

The script prepares a disposable database, sample canon, local bare Git
forge, temporary TLS certificate, executor API/identity/relay/work server,
and a container that imitates the Compute VM. It should exit 0 only when
all seven stages succeed:

1. Device enrollment, with no enrollment code persisted on the executor volume.
2. An agent turn through the real dsh harness and local model relay.
3. Delivery of its mail.
4. Server-measured `source=relay` costs, with device reports excluded from caps.
5. `draining`: leases released, `drained=true`, and working directory emptied.
6. `available`: a new lease and another successful turn.
7. Host revocation: executor exit code 6.

Keep the timestamped trial directory under `/srv/ameesh/lot100-trial`: its
`resultat.json`, `server.log`, `executeur.log`, `fournisseur.jsonl`, and the
exact Git head are the evidence. Inspect all seven `etapes` entries and
`mesures` (HTTP calls per turn and health latency); do not treat process
startup alone as a successful trial.

This container trial does not validate a real desktop VM launcher, actual
user inactivity, physical-device attestation, signed-image verification,
restricted network egress, a production forge, or a live provider. Those
remain separate deployment/integration checks. No command has been run on
78.232.55.189 as part of this review.
