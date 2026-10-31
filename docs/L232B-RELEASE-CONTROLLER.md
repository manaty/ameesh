# Release controller protocol (L232b)

`deploy/mise-a-jour/controleur-release.py` provides a durable ordering engine
for an **independent controller process**. It is not installed as a service,
contains no SSH/systemd implementation, and accepts no CLI request to deploy.
Host effects currently exist only as an injected interface tested with simulated
adapters. The read-only `autorite-release.py` adapter can revalidate a scoped
owner-signed decision through the existing decision/key/approval registry. It
is not enrolled on a host or installed as a service. A boolean supplied by a
runner is not an enrolled implementation of either interface.

The manifest carries a host reference, full commit, matching versioned-release
identity, canonical database identity, exact unit allowlist, policy reference
and rollback deadline. No credential values or connection strings are accepted.
The manifest digest binds every action and receipt. Private owned journal files
and a per-host process lock protect local ordering; a durable host reservation
also excludes another operation after the controller dies or an action fails.
The reservation is released only after acceptance or verified rollback.

The successful sequence is prepare → backup → pin → fence → migrate → configure
→ activate → restart → health → accept → unfence. Each action has a stable
`operation:step` identifier and a fsynced intent **before** its adapter call.
A retry asks the adapter to reconcile that identifier, including effects that
completed before a connection or receipt was lost. The engine never assumes
an exception means no effect happened. It records only exact bounded receipts;
provider exception text is replaced with a generic failure category.

The enrolled interfaces must provide the following guarantees before this
protocol can execute on a real host:

- `authority.validate(operation, digest, step)` verifies the host and payload
  against a scoped signed approval or standing delegation, expiry and any
  revocation. It runs before every action, including recovery. A recovery
  delegation must remain available for the rollback window. A policy name
  alone is not approval, and the engine does not verify signatures itself.
- `adapter.perform(step, request)` reconciles a stable action id and returns
  exactly the request plus `ok: true` after verified completion. `ok: false`
  means definitive failure. Timeouts, lost replies and ambiguous partial
  completion must raise and reconcile on retry, never fabricate a failed or
  successful receipt. The adapter persists its own operation-bound receipts.
- `prepare` verifies approved release contents, toolchain, capacity, host
  enrollment and unit allowlist; retains the former code/venv and configuration
  snapshot. That snapshot belongs to this operation, never a mutable global
  "previous" reference that another release can replace.
- `backup` completes isolated restore checks for every affected database and
  remote retrieval/restore where required. `pin` registers and protects the
  exact verified recovery set before migration; incomplete backup blocks all
  subsequent changes. Recovery leaves these sets protected.
- `fence` atomically prevents executor claims and drains active turns, without
  interrupting their work. `adapter.fenced(request)` checks the current owned
  fence and absence of active turns before every migration/switch/restart/health
  and rollback action; a recorded old fence receipt is insufficient.
- `migrate` holds a database-wide lock keyed by canonical database identity,
  shared across hosts/controllers, and reconciles already committed versions.
  Both dry-run and execution use the manifest commit with unconditional
  additive-only. Host-local serialization cannot elect that database worker.
  The migration library now elects a worker before bootstrap/inventory and
  commits each entire pass on the same backend as its transaction lock; the
  release CLI also rechecks its additive guard inside that election.
- `configure` validates and snapshots units, slices, drop-ins and guard policy.
  `activate` uses the single versioned code/venv pointer. `restart` is bounded.
  `health` verifies schema, doctor, actual cgroup caps, guard, CLI and one
  controlled authenticated turn, then drains that probe under the same fence.
- `accept` records release acceptance for retention. `unfence` atomically
  checks the owned fence on its first execution; it must replay its exact
  durable receipt if that execution removed the fence before losing the reply.
  Checking for a still-present fence before replay would strand completed
  operations. Fence disappearance without that receipt never proves success.

A definitive configure, activation, restart or health failure enters recovery:
recovery fence → rollback → recovery restart → recovery health → recovery
unfence. Rollback restores the operation's prior code, venv and configuration;
additive database migrations remain. Failed or ambiguous rollback retains the
reservation and fence. A failed migration is held for reconciliation rather
than automatically retried under a different release. No arbitrary reservation
reset or unfenced force path is exposed.

Tests cover ordering, failed backup, authority revocation, disappeared fences,
changed manifests, two simultaneous controllers, lost replies at every release
and recovery action, health rollback, rollback failure and receipt/log canaries.
They establish protocol behavior with simulated effects, not an authenticated
host pilot, root delegation, real claim fence or PostgreSQL lock integration.
The next integration must enroll authority and supply durable host receipts and
the bounded SSH/systemd adapters; helper/bootstrap and the controller's own
installer wiring are prepared as source (the operator entry below) but executed
on no host yet.

## Scoped signed decision adapter

`autorite-release.py.question(operation, manifest, expires_at=EPOCH)` prepares a
reviewable question containing the exact manifest, its digest, host, operation,
allowed release/recovery steps and execution deadline. Use that exact question
and the module's `OPTIONS` with `decide ask --needs-signature` only when the
host operation is fully prepared. This development checkpoint sends no request.
The expiry must cover the rollback window. A prepared question is not authority.

`SignedDecisionAuthority(db, decision_id, operation, manifest)` implements the
controller's validation interface. Only the signed `apply` option counts, as
`decisions.match_option` recognizes it: the raw key or the two fixed
`signer-owner.sh` mentions, the whole signed text staying recorded and digested.
Any other free-text answer, decline, pending decision, different operation,
changed manifest or unknown action does not. It re-reads the decision and runs
existing Ed25519 proof verification against the currently enrolled owner key.
Revocation, key replacement, a changed signed question and corrupted signatures
invalidate all subsequent release and recovery steps. The approval nonce must
have been consumed by this exact decision; this is not a new unconsumed
approval.

Both the deadline embedded in the signed question and the original signed
approval expiry must still be valid. The expiry row captured after the generic
proof check has its own signature reverified; replacing that row between reads
cannot extend the original signature window. Changes to the unsigned `due_ts` cannot
extend execution. Signed creation/expiry timestamps must match their database
rows; changing `expires_at` cannot resurrect authority. The operation id fixes
reuse to the same reconciled host operation; durable host receipts must still
prevent duplicate effects from two controllers. Standing delegations are not
implemented by this adapter.

Tests use a publicly known synthetic fixture seed and disposable PostgreSQL
schemas, never an actual owner's key. Tests cover real signatures,
consumption, expiry, row tampering, revocation/replacement and a full controller
sequence with a simulated host. Revocation after fencing stops before migration
while keeping the fence. These tests pass through both psycopg and psql.
The database connection, registry trust, clock and final effect-time authority
check must be enrolled independently; this read-only adapter does not bridge a
controller/helper privilege boundary or remove the need for host-side checks.

## Database migration worker

`migrations.migrate()` now takes the existing `ameesh_migrations` advisory
transaction lock before creating a schema/catalog or reading pending versions.
PostgreSQL scopes this lock to its actual database: different host names,
connection strings and schemas cannot elect simultaneous workers for that
database. Election respects the configured statement timeout. Only after
election is the SQL timeout lifted, matching the previous long-migration policy.

The same transaction/backend executes the full pending batch and commits its
version/checksum inventory. Failure or writer connection loss rolls back that
batch and releases its lock; previously committed passes remain. A retry reads
the inventory again under election, including after an uncertain commit reply.
Success messages are emitted only after commit. This replaces per-file commits:
a failure in migration N now also rolls back pending migrations 1..N-1 of the
same pass. A long pass holds its DDL locks until the whole batch commits.

`migrate --additive-only` retains its readable preflight, then passes the guard
to the elected worker for a second check. A changed inventory between those
checks cannot authorize a newly pending non-additive migration. Dry-run remains
read-only and is an observation rather than election or authority. The psql
transaction driver executes raw scripts on its existing backend, preserving SQL
format strings and avoiding a new connection that would bypass the lock.

The lock key remains compatible with the earlier per-file workers, excluding
their SQL while a new worker owns election. Earlier workers still read inventory
before their own lock and can later fail on stale inventory: upgrade every
migration worker before relying on reconciliation. This library integration does
not enroll controller/SSH adapters, select a migration identity or provide a
host claim fence. Transactional approved migration SQL remains a prerequisite.
`migrate` under the enrolled release-controller role is not supported: the
worker's `BOOTSTRAP` creates `schema_migrations` when it is absent, and that
role deliberately holds no `CREATE`, so it is insufficient by design. Granting
it `CREATE` would be a privilege change outside this work. Migrations run under
the schema owner's migration identity, an administrator prerequisite this work
does not select; a readable `has_database_privilege('CREATE')` pre-check that
refuses before any effect would be a useful addition, not a requirement.

Real disposable PostgreSQL tests cover competing workers before inventory,
fresh-schema and DDL rollback, additive refusal after an obsolete CLI preflight,
writer backend loss and retry, and bounded contention across schemas. Both
drivers are covered; no production connection or host action is performed.

## Durable host release fence

Migration 0417 adds a dedicated release fence to the existing volatile host
claim predicate. It is independent of placement withdrawal: installing it does
not create handoffs, return timers or new resource readings. Claim, interactive
attach, pending-prompt consumption, begin-turn and host-gate publications use
the same closed predicate. Existing turns finish; their leases, prompts,
sessions and placements are preserved. New agents registered on that host
also encounter the gate.

`fence-release.py.SignedReleaseFence` requires an independently enrolled
PostgreSQL connection and signed decision authority. Gate and agent-row locks
serialize publication with racing claim/turn writers. Authority is revalidated
after those waits and operation binding before writes. Closure commits while
turns drain; a bounded deadline leaves the gate closed for reconciliation.
Running status, unfinished prompts/accounting, and unfinished/orphan turn
resources prevent a drained receipt even if a lease has expired. The privileged
host helper must additionally prove actual process/cgroup quiescence: a database
observation cannot prove that every child process exited.

Operation/host/digest bindings and exact action receipts are durable in the
database. First unfence requires the live owned fence and quiescence; deleting
only that fence and saving its receipt are one transaction. Retry after a lost
reply replays the receipt, preserving any subsequent operation's fence. It never
clears a placement withdrawal or agent handoff flag. No expiry-based gate
opening or force-reset path is exposed; authority expiry/revocation retains
the fence until a separately prepared applicable recovery authority exists.

The creation migration removes inherited/default non-owner write grants from
the three control tables. Reapplying executor or canon-writer roles preserves
read access and removes their direct fence writes; the executor audit also
rejects inherited writes. `deploy/sql/role-release-controller.sql` prepares a
separate NOLOGIN group with read access needed for proof/drain validation,
registry row-lock access through UPDATE(updated_at) only, and writes to the fence
tables. Its audit rejects unrelated table/column writes, inherited roles and
schema/database creation. It grants no migration/backup/root authority and
creates no login or credential. This storage is deliberately outside the
mediated executor API. Enrollment remains a prepared administrative operation;
no role or migration is applied to a live mesh by this development work.

## Narrow restart host helper

`helper-redemarrage.py.RestartHelper` supplies restart and recovery restart
through one explicitly enrolled manager: the system manager or the user manager
of a named account, never inferred from a unit name. It is restricted to exact
enrolled `ameesh-runner@…` or `ameesh-runner-agent@…` instances. Its policy
identifies the host, allowlist, manager, independent controller service and
5–300 second restart budget. The signed manifest's policy reference must be
`policy-` plus the canonical policy digest, so changing that allowlist, manager
or budget invalidates operation authority. The controller service itself is
outside the allowlist and `system.slice` holds the action workers independently
of runner slices.

Before launch, the helper verifies current signed authority, the live drained
database fence, and cgroup process inventories. A runner's MainPID can remain;
any other process in its cgroup or descendants blocks restart, including an
orphan after its lease expired. Missing identities/inventories fail closed.
Authority is checked again after slow observations. These checks depend on
trusted system units and host/cgroup enrollment; they are not persona isolation.
The database gate/agent locks span the synchronous systemd submission, so a
concurrent authorized unfence must wait for that boundary instead of opening
claims between observation and submission.

A private fsynced intent precedes submission of a deterministic transient
systemd action service, with Type=oneshot, RemainAfterExit=yes and a bounded
TimeoutStartSec. The fixed action program is `systemctl restart` over the exact
approved unit list. No caller-selected shell, executable, path or service is
run. Unit Result, ExecMainStatus and InvocationID establish a completion receipt;
the action service must remain loaded until that receipt is durable. After a
lost SSH/submission reply or a crash before receipt publication, the same action
is observed rather than submitted again. A missing action unit or changed boot
identity with an incomplete intent requires reconciliation and holds the release;
it is not guessed to mean no effect occurred. Helper receipts prevent a second
restart, and raw systemd/provider exception text does not escape.

`hote-release.py` is a stdin dispatcher for a future root-owned forced command.
It requires the isolated immutable interpreter at
`/opt/ameesh-release/runtime/bin/python`, root-owned trusted source/runtime
paths, an exact enrolled machine-id and private root-owned files under
`/etc/ameesh-release`: enrollment.json (`host`, `machine_id`, `database`,
`policy`), database.json (explicit postgres backend and controller credentials),
and operations/OPERATION.json (`operation`, `decision_id`, full `manifest`).
The request only carries operation, action id, step and manifest digest; it
cannot supply units, credentials, manifests or filesystem paths. The protected
operation file supplies the manifest checked against the signed decide answer.
The exact pending action must also appear as the current intent in the private
root-owned independent controller journal under
`/var/lib/ameesh-release/controller`; a transport caller cannot skip the
controller's protocol steps merely by possessing the operation reference.
Ambient account/libpq/config/trace variables are removed before connecting.
Input is size/time bounded; errors return only a structured generic category.

The helper/dispatcher remain **source only**, not installed, exposed through SSH,
or enabled as a service. Tests simulate systemd and cgroup filesystem fixtures;
they do not prove a real host restart. The administrator bootstrap and helper
runtime, restricted SSH/sudo enrollment, independent service, config/caps/guard
snapshots and the controller's own installer wiring are prepared but not
installed; a real-host pilot, including the enrollment of the probe turn
(`turn.json`, assembled by `entree-operateur.py` below and installed by the
signed bootstrap inventory), is still required. This
checkpoint implements no user-mode systemd beyond the fixed helper path
described below and no other root operation; it grants no root authority to an
agent.

## Pinned SSH restart transport

`transport-release.py.SSHRestartTransport` implements only the `restart` and
`recovery_restart` transport calls. The independent controller must supply its
protected enrollment: logical host, DNS/IPv4 address, port, dedicated
`ameesh-release` user, host-key alias, private known-hosts file and its exact
SHA-256, private identity file, and a 5–360 second exchange budget. Key and
known-host files must be owned by the controller identity, mode 0600 beneath
ancestors owned by root or that identity without group/other write access.
Linked paths, changed known-host contents and caller-selected endpoints fail
before connection. Enrollment itself is not supplied by an action request.

The fixed `/usr/bin/ssh` invocation ignores account SSH configuration and the
global known-hosts store, requires the enrolled host-key match, disables host-key
updates, password/interactive authentication, identity agents, certificates,
forwarding, connection sharing and proxy commands, and uses one connection
attempt with bounded connect/liveness settings. The child receives a minimal
environment. Only the stable operation/action/step/digest JSON crosses stdin;
no remote command, shell, unit, manifest, credential value or path is sent.
An administrator must first install a forced command on the dedicated account
which invokes the fixed root dispatcher with the narrowly scoped sudo policy.
The dispatcher still checks its own protected manifest, signed authority, live
fence, cgroups and independent controller intent. A transport key is not release
authority. This module provides no enrollment or administrative installation.

Pipe input/output and process exit share the exchange deadline. Receipt output
is capped at 64 KiB; stderr is discarded. Deadline or output overflow kills and
reaps the local SSH process. That does not cancel an already submitted remote
systemd action: an exception leaves the controller intent unresolved. There is
no internal retry and no timeout-to-failure conversion. The next controller pass
uses the same action id and the helper reconciles its retained action/receipt.
Only a complete exact typed receipt, including a boolean outcome and the full
matching manifest, can advance the protocol; duplicate JSON keys are refused.
An exact `ok: false` remains a definitive helper failure for controller recovery.

Tests exercise local pipe peers for EOF, environment, flooding, nonzero exit,
stalled input/output and process exit after closing stdout. Controller/transport/
helper simulations lose a reply after both release and recovery restart, retain
the fence and intent, and complete on retry without a second systemd submission.
No SSH connection, authenticated host-key check or real systemd action is run.
This remains source-only and requires the bootstrap/runtime/forced command,
independent service and complete release adapter before a real host pilot.

## Prepared SSH/sudo delegation inventory

`delegation-release.py.render(plan)` prepares the exact root-owned forced
command wrapper, sudoers entry, public authorized-key file and SSH account Match
configuration. It performs no installation, creates no account and enables no
service. The plan binds the logical host, exact machine-id, independently
verified host-key digest, full runtime/source tree digests, restart policy,
one bare Ed25519 controller public key and a canonical controller IP network.
Credential values, authorized-key options/comments, shell text and arbitrary
paths are refused. Every output file has a fixed destination, ownership, mode,
content and SHA-256; the complete review inventory has its own digest.

The restart policy requires an explicit closed `manager` object: either
`{"kind":"system"}` or `{"kind":"user","account":"ameesh"}`. The account must
match `[a-z_][a-z0-9_-]{0,31}`. Missing or unknown fields are refused; existing
inventories must be regenerated and approved with the new policy digest.
One manager applies to every runner in that policy, independently of unit names.
For a user manager the root helper uses fixed `systemctl --machine=<account>@.host
--user show|restart` arguments. It walks the cgroup subtree reported for the
enrolled unit with the same process-inventory checks as for system runners; it
does not verify that the path lies under `user.slice` or any other slice. The
unit's cgroup is delegated to that account, which can move processes out of the
subtree, so the check bounds only the processes still visible in the reported
cgroup. The transient action job, its state observation and durable receipt
remain in the system manager. Neither manager nor account can be supplied by the
SSH request.

The public-key file is root-owned 0644 so the dedicated SSH account can read it;
the enclosing root-owned directory must be searchable. Enrollment, database,
operation and journal files remain root-private. Both the key and account force
the same wrapper. Account settings disable forwarding, TTY, user rc and alternate
password/interactive authentication. The wrapper refuses arguments and any
`SSH_ORIGINAL_COMMAND`, then clears the environment before the fixed sudo call.
The sudo grant names only the immutable interpreter with the exact isolated
dispatcher arguments with `-I -B`; it grants no shell, systemctl or arbitrary
Python program. The dispatcher requires both isolated mode and disabled bytecode
writes, preserving the pinned runtime/source inventory during startup.

`question(bundle)` prepares reviewable signed-decide text including the complete
digest-bound inventory and revocation plan. It does not send a question or
authorize root execution. The proposed revocation restores only approved prior
snapshots or removes exact newly created delegation files and preserves runtime, private operation/helper/controller
journals, credentials and live release fences. Changed files require a separately
reviewed diff. Revoking SSH access does not fabricate completion or reopen claims.

The renderer remains a preparation component. It now accepts exact numeric
account ids and a protected prior-file digest/mode inventory; absent declarations
mean files must not already exist. Directory modes and SSH/controller service
references enter the review digest. Rendering performs no installation.

## Signed bootstrap and administrator host adapter

`bootstrap-release.py` supplies a separate owner-signed bootstrap scope. Its
compact question binds operation, host/machine-id, exact uid/gid, host key,
controller key/network, runtime/source trees, file/prior-file inventories,
directories, services, revocation and action scopes. The full immutable review
bundle must be available beside that question; its digest binds the contents
while the question stays within `decide`'s 4000-character limit. No question is
sent. Only a signed `install` counts; current owner key, execution deadline,
consumed nonce and the captured original approval signature/expiry are rechecked
using the same proof validator as the release adapter. Account scope is recomputed
from exact machine-id/operation/uid/gid; tree scopes name fixed destinations.

The durable sequence is preflight → layout → runtime → sources → account →
configuration → validate → enable. A reservation excludes another unresolved
bootstrap; exact intents precede effects. Lost replies reconcile their original
effects. Expiry/failure preserves reservations and receipts. Key activation is
last. The separately signed `revoke` action is not part of installation.

`installateur-bootstrap.py.BootstrapHostAdapter` connects the concrete
tree/account/file primitives. Inputs are staged under the fixed administrator
path `/var/lib/ameesh-release-bootstrap/OPERATION/` as `runtime` and `sources`,
never a transport-selected path. That root is itself part of the signed
directory inventory at mode 0700: `layout` creates or validates it instead of
leaving an ad hoc staging directory outside the reviewed plan. `LinuxHost`
requires root, checks machine-id and
the SHA-256 of the SSH Ed25519 host-key wire blob, uses fixed directory/account
commands, and validates sudo syntax and effective SSH settings before reloading
the fixed `sshd.service`. Revocation validates/reloads the restored settings.
The service alias, SSH drop-in inclusion and public-key login behavior must be
verified on an admitted host. Journal directories are private; the root-owned
SSH home is searchable without account write access.

`runtime-release.py` inventories bytes and executable modes with explicit
byte/entry limits (128 MiB and 20000 entries per tree by default). Symlinks,
hardlinks, special files, unsafe modes/ownership, read races and low disk capacity
fail admission. A private intent precedes copy, fsync, read-only modes and atomic
rename. Changed inputs or partial stages remain for inspection. A rename followed
by a lost receipt replays its own verified tree without a second copy or needing
the original staging source. Unowned destinations are never overwritten.
Authority is checked after slow reads/copy and before publication/replay.
This pins supplied trees, **not completeness of external Python/ELF dependencies**;
independently pinned immutable toolchain closure remains an administrator admission
prerequisite. Tests use synthetic executable bytes, not a production interpreter.

`construction-runtime.py` builds the supplied `runtime` tree, outside root and
in a new directory, from one exact SHA: a fresh `/usr/bin/python3` venv, the
ameesh wheel built from a `git archive` of that commit, and its dependency
closure (`psycopg[binary]`, `psutil`) installed with
`--require-hashes --only-binary=:all:` from `runtime-pins.txt`; the build
backend is pinned separately in `runtime-build-pins.txt`. The clone must be
clean and at exactly that SHA. The command writes a provenance receipt (SHA,
both pin digests, Python version, wheel digest and measured tree digest) beside
the read-only runtime, and never installs a root artifact or publishes
anything. The administrator still reviews the closure and stages the measured
tree root-owned (`runtime-release.py` requires the staged tree to belong to the
installing root), not with the builder's ownership.

`construction-runtime.py --assets` (#4786) writes, from the same commit, the
release assets that publish this provenance: the same wheel, both pin files
byte for byte and `SHA256SUMS` over the three. The 11:00/23:00 cutoff
(`release_cutoff.ensure_github_release`) runs the release commit's own
`construction-runtime.py --assets`, checks `SHA256SUMS` and attaches the assets
to the GitHub release it creates; a manual `poste.sh publier` builds them
before the tag. Versions older than `runtime-pins.txt` have none. A failed
asset build at the cutoff is a report deviation: the release is created without
assets (staging is not held), and a release created before its assets is not
completed afterwards. A runtime root is admitted for real production only
against the assets of its release, so such a release is never admitted.

`compte-release.py` creates the dedicated group/account with exact numeric ids,
no home creation or supplementary groups, a fixed forced-command shell and an
impossible password sentinel. Shadow values are not persisted. Group/user intents
precede commands; lost replies reconcile exact observed identities. A missing
identity after an ambiguous command is not recreated. Existing unrelated numeric
ids/accounts, changed shell/groups/password status or revoked authority hold the
bootstrap. Undo never deletes an account.

`fichiers-bootstrap.py` privately snapshots exact approved prior bytes/modes
before replacement and rejects unreviewed existing files. Lost receipts reconcile
actual installed contents. Revocation starts with the key, restores only approved
snapshots or removes exact new files, and preserves external changes, runtime,
account, credentials, journals and live fences. Authority is rechecked after the
final file/readiness observations before each effect.

Activation requires an active independent controller in `system.slice` and a
root-private readiness receipt bound to host/enrollment, current boot/systemd
invocation and verified read-only runtime/source trees. The service coordinator,
fixed unit, pinned host factory and its two pinned companions are now prepared
as described below; the recovery-set and health companions refuse admission
until their enrolled documents and private state exist. There is no automatic
root executor or runtime/credential enrollment. The operator entry below
assembles the reviewed bundle and the probe enrollment; execution belongs to
the enrolled administrator backend (`backend-administrateur.py`, below), run
by the administrator as root. No real root/account/SSH/systemd
effect is executed by this work. Tests combine disposable filesystem copies with
simulated accounts/services and synthetic real signatures on disposable PostgreSQL
through both drivers. Authenticated host pilot and external toolchain admission
remain required.

## Operator entry and probe turn enrollment

`entree-operateur.py` is the source-side assembly the signed bootstrap was
missing, and it is deliberately not a CLI. `prepare()` measures the staged
`runtime` and `sources` trees with `runtime-release.py` and refuses a plan whose
pinned digests differ — no silent repinning — then requires the host's observed
inventory of every reviewed destination (`previous_files`), renders the
canonical `delegation-release.py` review and returns the compact owner-signed
question (bounded by `decide`'s 4000 characters). `install()` hands that
reviewed bundle to the enrolled administrator backend (`bootstrap.run` over
`installateur-bootstrap.py`'s host adapter) with the enrolled authority and its
private journal; it accepts no caller-selected CLI, root, program or
destination. `installateur-bootstrap.py` keeps refusing both, because the entry
adds no route that could choose them.

The probe turn is enrolled as one of the reviewed files. `pilote-sante.py`
exposes the single `validate_enrollment` validator, `probe()` builds the exact
`turn.json` document only for an agent whose registry row is on this exact host
and whose own runner unit belongs to the enrolled restart policy, and
`delegation-release.py` accepts such a document only at its fixed destination
(`/etc/ameesh-release/turn.json`, mode 0600) as compact sorted JSON bounded to
8 KiB. `prepare()` does not take that document on trust: as soon as a probe is
enrolled it re-attests it through `enrolled_probe()` — the same `probe()` path,
with the plan's host and restart policy and the enrolled registry (`db`) — so a
document of another host or of an absent agent refuses before the question is
rendered. Its digest enters the signed file inventory, so the signed question binds
the exact probe identity; its bytes are installed with the same private
snapshot, lost-reply reconciliation and revoke semantics as the other reviewed
files (revoke removes or restores it like any other newly created reviewed
file). The controller key is still enabled last, and `HostFactory.admit` then
finds a `turn.json` shaped exactly as `HealthTurn` reads it. A missing,
divergent or unreadable registry refuses; `prepare()` never invents an
enrollment for an absent agent.

Assembly and enrollment remain source only: no host is contacted, no root,
SSH, systemd, migration or production effect is executed and no signed request
is sent. A real-host pilot still needs the enrolled administrator backend, the
administrator-staged trees and the owner's signed `install` decision.

## Enrolled administrator backend

`backend-administrateur.py` is the one root entry of the signed bootstrap
(#4323). It adds no effect of its own: it wires `SignedBootstrapAuthority`,
`installateur-bootstrap.LinuxHost`/`BootstrapHostAdapter` and `bootstrap.run`
(through `entree-operateur.install`) to fixed paths. Its arguments are a
command, an operation name and, for signed commands, a decision id; no
program, root, destination, journal or credential is selectable.

| Command | Effect |
| --- | --- |
| `inventory OPERATION` | none: host inventory of the reviewed destinations of the staged plan, machine-id, host-key digest |
| `prepare OPERATION` | no host installation effect: measures the staged trees, observes the inventory itself, writes `question.txt` then `bundle.json` (0600) beside the stage; an existing different bundle refuses |
| `plan OPERATION --decision N` | none (dry run, no `adapter.perform` nor `bootstrap.run`): ordered steps, scope digests, per-step authority, observed state, revoke preview and whether N authorizes the revoke; exit 3 when N authorizes neither |
| `install OPERATION --decision N` | the signed bootstrap, resumable from its journal |
| `revoke-question OPERATION --expires T` | none: prints the separate rollback question (options `revoke`/`leave`) |
| `revoke OPERATION --decision N` | rollback under its **own** signed decision (`SignedBootstrapRevokeAuthority`), under the bootstrap host lock: key first, approved snapshots restored, exact new files removed; runtime, account and journals kept |

An `install` answer never authorizes `revoke`, and a `revoke` answer never
authorizes an install step: a root rollback is not implied by the decision
that installed.

The administrator stages `runtime/`, `sources/` and `plan.json` (exactly
`plan`, `account_ids`, `expires_at`, optional `probe_turn`, `runtime_seal`,
root 0600) under `/var/lib/ameesh-release-bootstrap/OPERATION/`. The question
is then asked with `ameesh decide ask --needs-signature`; the owner signs
`install`, raw or with the fixed `signer-owner.sh` mention, and the backend
recognizes the option through `decisions.match_option`, never a free prefix.
Private state lives under `/var/lib/ameesh-release-bootstrap/.admin`
(no operation name starts with a dot): the controller and effect journals and
`gestes.jsonl`, an append-only journal hash-chained line by line (`seq`,
`prev`, `sha`) carrying the backend code digest, bundle digest, step, intent,
receipt digest or a fixed failure category, never a document body, secret or
provider text. A removed, reordered or edited line refuses every later gesture.

Enrollment, by the administrator before the first gesture:
`/etc/ameesh-admin` root 0700 holds `backend.json` (`version: 1`, `host`,
`machine_id`, `code_sha256` of the installed backend and companion files,
root 0600; a different digest refuses) and `config.json`, an ameesh configuration naming the
separately enrolled controller database connection (root 0600), never the
runners' settings. The backend runs from the reviewed wheel of the pinned SHA
installed in a root-owned environment outside any agent checkout (a
non-root-owned code directory refuses), `PYTHON -I -B
…/share/ameesh/mise-a-jour/backend-administrateur.py`; a staged plan or
bundle of another host, an invalid operation name (refused before any path
is built), a non-root caller, an altered question, an absent,
unsigned, expired or revoked mandate all refuse before any host effect. Tests
use a disposable root and simulated accounts/services, and the real signed
authority on disposable PostgreSQL; the qualification on a real host starts
with `plan` on agents-1.

### Channel accounts bundle (#4327)

The release bundle enrolls only `ameesh-release`. The canary pair (#3392)
also needs the dedicated `ameesh-handoff` (H2) and `ameesh-secrets` (L232c)
accounts: a second, separately signed bundle, `delegation-canaux.py`, renders
both together (both are required, with distinct uid/gid and one distinct
controller key and source per channel):

| Object | Exact value |
| --- | --- |
| Accounts | `ameesh-handoff`, `ameesh-secrets`: system, `/bin/sh`, password `*`, no supplementary group; home = the channel's root-owned `/etc` root (never writable by the account) |
| Roots | `/etc/ameesh-handoff`, `/etc/ameesh-secrets` root 0755 (key lookup by sshd); `/var/lib/ameesh-handoff`, `/var/lib/ameesh-secrets` root 0700; an existing root must already have this mode |
| Handoff channel | `/usr/local/libexec/ameesh-handoff-ssh`, `/etc/sudoers.d/ameesh-handoff`, `/etc/ameesh-handoff/authorized_keys`, `/etc/ssh/sshd_config.d/62-ameesh-handoff.conf`, dispatcher `/opt/ameesh-release/lib/hote-handoff.py` |
| Secret channel | the exact files of `delegation-secrets.py` (61-ameesh-secrets, dispatcher `hote-secrets.py`) |

No tree is published and no enrollment document (`execution.json`,
`policy.json`, `helper.json`, transports) is part of it: the dispatchers run
from the release runtime and sources, which must already be installed at the
plan's pinned digests, and each refuses until its documents are enrolled by
their own decision. `bootstrap-canaux.py` orders `preflight` (machine-id,
host key, installed release trees read-only, dispatchers present), `layout`,
`account_handoff`, `account_secrets`, `configuration`, `validate` (`visudo`
and `sshd -T` for each account with its own source), `enable` (reload,
revalidate, both keys last) with the release engine, journal and host
reservation; its question prefix, options and rollback decision
(`SignedChannelRevokeAuthority`) are its own, so a release decision never
authorizes a channel step and conversely. `installateur-canaux.py` holds the
`LinuxChannels` commands and the `ChannelHostAdapter`.

The administrator stages only `plan.json` with `"delegation": "canaux"`,
`plan` (`host`, `machine_id`, `host_key_sha256`, `runtime_tree_sha256`,
`source_tree_sha256`, `channels.{handoff,secrets}.{controller_source,
controller_key}`), `account_ids.{handoff,secrets}.{uid,gid}` and
`expires_at` (`probe_turn` and `runtime_seal` refused); the same backend
commands then apply, `plan` reports `"delegation": "canaux"`.

## Independent service coordinator

`service-release.py` coordinates protected operation files from the fixed
`/etc/ameesh-release/operations` directory and writes private controller intents,
results and readiness under `/var/lib/ameesh-release/controller`. Each operation
has only its stable id, positive decision id and full manifest. Host/database,
signed policy identity and units must match independently protected enrollment;
there are no executable, shell, handler or credential paths in a job. The service
creates a fresh signed decision authority for each operation, and the existing
controller checks it and live fences before effects. A held host reservation
takes priority over other queued jobs. Uncertain replies retain that reservation
and retry the same action after a bounded backoff; process restart reconciles
the durable controller journal. Completed jobs do not repeat effects.

The `unite-controleur.py` unit is included in the bootstrap's exact file inventory
at `/etc/systemd/system/ameesh-release-controller.service`. It runs the fixed
isolated interpreter with `-I -B`, root identity, private umask and `system.slice`,
outside every allowed runner instance. Runtime/source trees are mounted read-only
inside its namespace. No runner environment file, template instance, transport
argument or caller-selected command changes its entrypoint. Installation and
startup remain administrative actions; this work enables no service.

The real entrypoint requires the exact runtime, protected enrollment and separate
`service.json` containing the enrolled runtime/source tree digests. It verifies
read-only tree inventories before importing any host factory/database components
and clears ambient account/libpq/config/trace environment. The sole factory is
the fixed pinned source `adaptateur-hote-release.py` with `HostFactory(db,
enrollment)`, never a module selected by JSON. Its admission must establish all
release and recovery capabilities after actual backend/credential/config checks;
a boolean, partial capability set or extra arbitrary operation is refused.

Before readiness publication, the process checks actual systemd MainPID against
its own PID, loaded/active state, independent slice, current boot/invocation,
machine-id, protected policy and exact runtime/source trees. Identity/trees are
checked again after factory admission. Readiness is withdrawn before readmission
and on owned shutdown; an older instance cannot remove a later instance's
receipt. Errors emit generic categories and typed operation references, never
private documents or provider/credential exception text.

Tests use simulated factory/systemd effects and private disposable filesystem
documents. They verify stable receipts across process replacement, failed
admission, code drift, lost replies, reservation priority, wrong jobs, backoff,
withdrawal ownership and generic diagnostics. They establish coordinator behavior,
not a launched system service or authenticated host pilot.

## Pinned host factory

`adaptateur-hote-release.py` is the pinned concrete factory. It is fixed source
loaded by the service, never a module named by a job, transport message or
enrollment document. It composes the protected documents under
`/etc/ameesh-release` — `card.json` (effective configuration card),
`destinations.json` (bounded destination map), `transport.json` (SSH restart
endpoint) and `backup.json` (recovery identities, retention window, target
volume admission and optional remote references) — with the fixed private
state under `/var/lib/ameesh-release` and the versioned release store under
`/opt/ameesh-release/store`. The store root is
part of the signed bootstrap directory inventory at mode 0700, like the
administrator staging root; it is never created ad hoc. Enrollment, database
and operation documents keep the exact shapes checked by the dispatcher and the
service. A missing, linked, group/world-writable or inexact document refuses the
factory before any host effect; document bodies and credential values never
enter an error.

`HostFactory.admit(enrollment)` returns exactly the controller's step set only
after the real checks: exact enrollment and host binding, restart policy
validation, host-card validation, every enrolled runner unit inside that card,
configuration destinations, the SSH endpoint files and their host-key digest,
private configuration/backup/retention state directories, fixed root-owned
`systemctl`/`systemd-analyze` paths, a live database ping and a read of the
release-fence receipt table. Admission
is honest: every `prepare`, `backup`, `pin`, `fence`, `migrate`, `configure`,
`activate`, `restart`, `health`, `accept`, `unfence` and recovery step must be
bound to a concrete capability, and the service refuses a partial set instead of
admitting simulated backends.

`for_operation(job, authority)` returns one operation's adapter. It revalidates
authority before every effect, delegates `fence`, `unfence` and their recovery
steps to the signed fence, `restart` and `recovery_restart` to the pinned SSH
transport, `configure` and `rollback` to the configuration journal plus the
effective systemd/caps/guard verification, `activate`/`rollback` to the versioned
release pointer, `prepare` to the completed release receipt, destination
inventory, unit allowlist and disk floor, `backup`/`pin`/`accept` to the pinned
recovery set and `health`/`recovery_health` to the pinned health probe, and
`migrate` to the release source's
migration directory with unconditional additive-only election. A definitive
refusal becomes the controller's `ok: false`; an ambiguous or unauthorized
failure raises so the durable intent reconciles instead of repeating an effect.
Provider exceptions and their text are replaced by a generic category.

The three companion pinned sources are supplied by this work: `sauvegarde-release.py`
exposing `RecoverySet(db, store=, root=, retention=, config=)` for `backup`,
`pin` and `accept`; `sante-release.py` exposing `HealthProbe(db, store=,
card=, system=, turn=)` for `health` and `recovery_health`; and
`pilote-sante.py` exposing `HealthTurn(db, enrollment=, host=, system=)` for
the controlled authenticated turn. All answer True after verified completion,
False on a definitive refusal, or raise for an ambiguous effect; the factory
refuses admission while any is absent or its enrollment is missing.

Recovery sets use an optional protected `backup.json` document (exact keys
`dsn`, `restore_dsn`, `retention_seconds`, `restore_capacity`, `remote`): the
dump and the isolated disposable restore use separate identities,
`restore_capacity` references the private file the enrolled producer refreshes
with the free space of the exact restore target's volume, `remote` (when
enrolled) names the object host plus one private upload credential file and one
private recovery credential file, and no credential value enters a receipt, an
argument, the child environment or an error. `dsn` must name the exact database
of the release connection the migrations open (an enrollment pointing at
another database is refused), and the pinned inventory carries that connection's
schema, so the set can only be the pre-migration image of the migrated database. Reading the effective target uses libpq's own parser
(`psycopg.conninfo`); when that parser is absent (the `psql` driver installs no
psycopg) the recovery set refuses at construction with the named
`LibpqParserUnavailable`, so admission stays fail-closed instead of accepting an
unverified target. The enrolled `dsn` and the release connection's DSN are
compared after libpq parsing: the effective `host`, `hostaddr`, `port` and
`dbname` must be textually equal. Absent network fields fall back respectively
to `PGHOST`, `PGHOSTADDR` and `PGPORT` (then empty host/hostaddr and port `5432`);
`dbname` must be supplied in the DSN. The backup may therefore carry its own
credentials, but not another spelling of the same server (`127.0.0.1` for
`localhost`, a different host case). An omitted host can match an explicit host
when `PGHOST` supplies that same value; `service`/`PGSERVICE` is refused.
`backup` publishes the dump and its pinned row inventory in the private
retention root, proves an isolated
restore against that inventory, proves a configured remote copy can be
retrieved with the recovery identity, and records the verified set in a
private receipt. A fresh restore and a remote retrieval are admitted before
any copy: the target cluster's volume is not readable from the client, so the
live attestation (exact keys `target_sha256` of the enrolled `restore_dsn`,
`free_bytes`, `checked_at` as integer epoch seconds; refreshed at the step,
unusable after five minutes, `CAPACITY_TTL_SECONDS`) must cover twice the
pinned uncompressed source size
(`source_bytes` in the inventory) on top of the release disk floor; a missing,
stale, unsafe or differently bound attestation is refused the same way. An
inventory that carries no `source_bytes` (written before the K2 bound) is
refused identically: the pair is recreated from the live source (a re-dump)
before any copy, and no operator-declared size is accepted. The
private staging download reserves the artifact size. Insufficient or
unattested space is a definitive refusal, never a partial copy. `pin`
registers those exact bytes before migration with the manifest rollback deadline
and the enrolled retention window; an unaccepted set is never expired. `accept`
records release acceptance, drops the transient receipt and then expires the
previously accepted sets whose policy window has passed; a held set, or one
whose remote members have no enrolled deletion adapter, stays protected and
the expiry is retried at the next acceptance. An interrupted publication is
recreated from the live source; an artifact left by an interrupted attempt is
only reused while it is inside the bounded reuse window — one hour
(`REUSE_SECONDS` in `sauvegarde-release.py`): the receipt is bound to the
operation and its manifest, not to the approval that asked for it, so under a
single approval the drift stays below that bound and a set resumed days later
is recreated, never pinned stale. A receipt that is
missing, stale, from another manifest or no longer matching its files never
holds `pin`: the set is recreated from the live source before registration —
the step still runs
before any fence or migration, under the current approval — so a new approval
never inscribes a dump made under an earlier one and the resume cannot wedge on
a refusal that time only makes truer. Nothing durable precedes the fresh
receipt, so a crash or a failed dump during the recreation is replayed by the
next `pin`; a verified receipt, once written, survives a lost reply without a
second dump, restore or upload. An unreadable, invalid or unsafe receipt is
still refused protectively by the receipt reader; it is not recreated.

`HealthProbe` rederives the active release's own completed receipt, checks the
versioned pointer (`health`: the activated release; `recovery_health`: the prior
release restored by rollback), the released schema against the release source's
migrations, the effective card units, declared capabilities and guard, and runs
`doctor --probe` from that release's own pinned interpreter with a private
credential file, scrubbed environment and bounded deadline. It then requires the
controlled authenticated turn from `HealthTurn` and never claims one it did not
receive.

`turn.json` names the enrolled probe agent, its own runner unit, the executor
account and home, and a bounded deadline (5 to 120 seconds). `TurnRunner` builds
the fixed one-turn command from them: the active release's own
`agent-runner --once --wait N` as that account, through the fixed `runuser`, no
shell, scrubbed environment and a bounded process. `HealthTurn` refuses to
disturb a live lease or a running turn, and accepts the turn only with exactly
one more completed turn, a newer claim generation, a completion timestamp and a
closed turn resource, with nothing of the probe left running. A missing runner,
a no-turn exit or divergent evidence is a definitive refusal; an ambiguous
result raises. Tests use real disposable documents, release trees and private
roots with simulated systemd, database and provider effects; no root, SSH,
migration or production effect is executed.

## Operation-specific code and environment recovery

Before configuration changes, the controller can use
`environnement-versionne.py --store STORE pin-switch --operation OP --release ID`
to persist the exact prior and target pair in a private operation receipt.
The current pair and target must both have completed versioned-release receipts;
a first installation without a prior release needs a separate bootstrap plan.
Repeat pinning preserves the prior pair and rejects another target for that id.

Pass the same `--operation OP` to `activate` and `rollback`. Activation permits
only the pinned prior or target as current; rollback restores the pinned prior
without consulting the shared legacy `switch.json`. A configuration failure
before code activation can therefore still recover its prior pair. A third
release installed in the meantime blocks either switch rather than being undone.
Lost activation replies reconcile the actual pointer with the operation receipt.
No pip command runs during rollback. Configuration snapshots, claims and signed
authority still belong to the enrolled host adapter; `--fenced` remains a caller
assertion in this utility, not proof of a fence or permission to deploy.

## Operation-specific configuration file recovery

`configuration-release.py` now prepares the file portion of configuration
recovery. It has no CLI and is not yet wired into `HostFactory`. Administrator
enrollment supplies a bounded, distinct destination map under
`/etc/systemd/system` and `/etc/ameesh`; jobs supply no destination paths.
User-owned desktop units need a separate enrollment policy. The versioned-release
build manifest must contain the exact `configuration` inventory of named UTF-8
contents and modes. Its full digest must match the release identity in the signed
operation manifest, using the same encoding as `environnement-versionne.py`.
Changing any target byte therefore invalidates that signed release binding.

The `pin` action saves every prior byte and mode, including absent files, in one
private durable operation journal before any replacement. Repeat pinning retains
that original snapshot. Configure and rollback require signed authority and a
live drained database gate held throughout file publication and journal updates.
Authority is checked again after slow writes and after acquiring the gate;
rollback also checks its signed deadline. Writes use private temporary files,
atomic replacement and directory fsync. Links, hardlinks, unsafe ownership or
modes, unexpected inventory and oversized files are refused.

Lost publication or removal receipts reconcile the actual target/prior bytes
without rewriting completed effects. Partial configuration can restore both
written and untouched files. Rollback restores exact original modes and deletes
only an operation's newly created files; unrelated content or mode changes hold
recovery instead of being overwritten. Configure cannot resume after recovery has
started. Configuration bytes stay in private journals, outside controller results.

These are **file receipts only**, deliberately without the controller's `ok`
field. The host adapter must still validate syntax, reconcile daemon reloads and
verify effective units, caps and guard policy before returning a full configure
or rollback receipt; `verification-release.py` (below) now supplies that
effectiveness half and the pinned factory binds both halves. The recovery-set,
health and authenticated-turn companions are supplied too (above); the
controller's own installer wiring and the probe enrollment are assembled as
source by `entree-operateur.py` (above) but executed on no host yet — the
real-host enrollment of the probe turn still needs the enrolled administrator
backend (the daily retention provider is wired separately,
`L232B-PRIVATE-BACKUPS.md`). Targeted
tests exercise real disposable files with simulated signed authority and database
gates; no real host configuration or service is changed.

## Effective configuration verification

`verification-release.py` completes the host half of configuration recovery. It
has no CLI and accepts no job-supplied command: the pinned factory supplies a
protected host card and the system adapter. `HostConfiguration(files, card,
system)` consumes the same `ConfigurationFiles` journal and release document as
configuration recovery, so published bytes and their effective systemd view
share one signed identity.

The card is administrator enrollment: the exact unit set whose effective state
must hold after any configuration change (`units`), declared systemd properties
compared byte for byte (`properties`), an optional active guard unit (`guard`)
and a bounded command deadline (`seconds`, 5 to 120). Every release manifest
unit must belong to the card; a card property outside those units, an unknown
property name, an unsafe value, a unit outside the card or an unbounded deadline
is refused before any system command runs.

For `configure`, every enrolled destination must still carry the exact published
bytes; for `rollback`, an absent document is the expected restored state.
Enrolled unit files are checked with the fixed root-owned `systemd-analyze
verify`, then `systemctl daemon-reload` runs, and authority is rechecked after
that slow command. Every card unit is read with the fixed `systemctl show`: it
must be loaded, `NeedDaemonReload` must be `no`, its fragment and every drop-in
must resolve to a regular root-owned non-linked file that is not group- or
world-writable, and declared properties must match exactly. A declared guard
unit must be active. Commands use absolute fixed executables, a scrubbed
environment, bounded output and a bounded deadline; systemd output never enters
a receipt or an error.

Receipts are counts and booleans only (`units`, `guard`, `reloaded`,
`syntax_checked`, `documents_checked`) — deliberately without the controller's
`ok` field, which the host factory adds once its remaining adapter obligations
are met. Tests use real disposable files and simulated systemd; no host unit,
daemon, guard or service is touched.

## Separate controller enrollment operation

`enroler-controleur.py` prepares the exact private document inventory and a
separate owner-signed question before publishing the seven fixed controller
documents and starting its existing independent system unit. A distinct signed
key generation phase creates the new root-private host-local transport key;
only its public key and fingerprint enter the enrollment mandate. It never writes
readiness, exports a private credential, changes an SSH delegation or modifies an immutable
runtime/source tree. Its separate signed stop retains documents and journals.
See [the operator procedure](ops/release-controller-enrollment.md) for document
schemas, secret provenance, durable resume, proof collection and the unresolved
pilot transport/delegation prerequisites. The administrator must enroll the new
backend code and supply the private inputs before this entry can run on a host.

### Dedicated restore volume and capacity producer (#5165)

`deploy/mise-a-jour/restauration-release.py` is an administrator-only installer,
probe and remover for the disposable recovery cluster. It is independent of
Debian's default cluster and of the live mesh database: it uses PostgreSQL's
`initdb`/`pg_ctl` directly and never changes a `main` cluster or its service.
The bureau runs it on the controller host after delivery through a package.
The host and PostgreSQL major version are enrollment choices; no host name is
compiled into the script. Install an already available PostgreSQL major that
can restore the source dump (at least the source server's major), and an
administrator-owned Python environment with `psycopg`. The installer does not
install packages or fetch dependencies.

For example, with PostgreSQL 16 and the controller's protected backup document:

```sh
sudo /opt/ameesh-release/runtime/bin/python3 -I \
  deploy/mise-a-jour/restauration-release.py install \
  --pg-version 16 --backup /etc/ameesh-release/backup.json \
  --python /opt/ameesh-release/runtime/bin/python3 --restore-role release_restore
```

The restore role must match the login chosen for #5162; it is an explicit
installation parameter, restricted to a plain lowercase SQL identifier.

The installed script is `/opt/ameesh-release/restore/restauration-release.py`.
Its cluster has these fixed boundaries:

- OS account `ameesh-restore`, without a login shell, owns only the disposable
  data at `/var/lib/ameesh-release-restore/<major>/data`;
- configuration is root-owned under `/etc/ameesh-release-restore/`, outside
  that account's writable tree;
- TCP is disabled (`listen_addresses = ''`); the only endpoint is
  the private socket `/run/ameesh-release-restore` is mode 0700 and is
  accessible only to the dedicated OS account and root. Runners receive
  neither this socket nor the restore identity's credentials;
- `ameesh-release-restore.service` runs PostgreSQL as that OS account, with a
  read-only system view and write access to its private data and socket;
- `ameesh-release-restore-capacity.service` runs as root and its timer refreshes
  once per minute. Each invocation is bounded to 120 seconds. A failed probe
  leaves the previous attestation unchanged; the consumer refuses it after
  300 seconds, rather than treating a failed probe as fresh evidence.

The identity lot #5162 enrolls a separate PostgreSQL role with `LOGIN CREATEDB`
(and no superuser, replication or role-administration privilege) on this
cluster. Local maintenance is available to the bureau through:

```sh
sudo runuser -u ameesh-restore -- \
  /usr/lib/postgresql/16/bin/psql -X -w \
  -h /run/ameesh-release-restore -d postgres
```

The installer creates `release_restore_admin` as a separate SQL superuser with
LOGIN and no password. Root uses `restore_owner_dsn` with
`host=/run/ameesh-release-restore dbname=postgres user=release_restore_admin`.
The exact HBA rules (with the selected restore role) are:

```text
local all ameesh-restore peer
local all release_restore_admin peer map=release_admin
local all release_restore scram-sha-256
```

The only `pg_ident` entry is `release_admin root release_restore_admin`.
There are no host rules. The first rule serves the maintenance probe; the
second serves #5162 as root; SCRAM permits the restore login to connect to
random databases created by verification. Port 5432 only names the socket
file `.s.PGSQL.5432`; no TCP port is bound.

Its `backup.json.restore_dsn` must explicitly name
`host=/run/ameesh-release-restore`, `dbname=postgres`, the separate restore
role and `require_auth=scram-sha-256`, with no port parameter. Credentials stay
in protected enrollment, never command arguments. Service references,
hostaddr, options, multiple hosts, TCP hosts and environment routing overrides
are refused. The exact enrolled DSN bytes are hashed into the attestation.
`backup.json.restore_capacity` names an absolute private root-owned output,
for example `/var/lib/ameesh-release-restore/capacity.json`. This directory is
root-owned, mode 0755; the attestation itself is root 0600. The recovery
consumer must run as root to read it, as the independent controller does;
copying it to a runner is not part of enrollment. The producer refuses an output
naming the probe enrollment, the install intent, `backup.json`, a remote
credential file, or any path under `/etc/ameesh-release`, the cluster
configuration and data directories, the install tree or the systemd unit
directory: a misdirected reference fails, it never replaces a protected
document (#5170). The consumer refuses, before any restore, when its own
environment carries libpq routing defaults the attested DSN leaves open
(`PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGHOSTADDR`, `PGPORT`,
`PGOPTIONS`): the in-process connection and `pg_restore` inherit them.

The root-private `/etc/ameesh-release/restore-probe.json` pins the backup
configuration path, PostgreSQL major and cluster `system_identifier` observed
at provisioning. A separate root-private install intent allows reentry on the
same installation without reinitializing data, and refuses to adopt a
preexisting unenrolled data tree. The probe connects through the private peer
socket, verifies that identifier, the actual `data_directory`, port and listen
address, and refuses external tablespaces. It reads `statvfs` on that verified
real data directory (`f_bavail * f_frsize`, excluding reserved blocks). It then
atomically replaces the attestation with mode 0600, exact fields
`target_sha256` (SHA-256 of the exact enrolled restore DSN), `free_bytes` and
integer epoch `checked_at`. It includes no DSN, credential or database content.
The existing consumer enforces the five-minute TTL, target hash, disk floor
and twice the source size before any restore copy.

After #5162 enrolls the restore identity and backup document, the bureau can
run `systemctl start ameesh-release-restore-capacity.service` to obtain the
first attestation immediately and check a real dump/restore through the
controller. Qualification in a development checkout uses simulated host
commands and real producer/consumer documents; it does not provision a host.

Removal is explicit and deletes only the enrolled disposable cluster:

```sh
sudo /opt/ameesh-release/runtime/bin/python3 -I \
  /opt/ameesh-release/restore/restauration-release.py remove
```

It checks the offline cluster identifier before deletion, stops the capacity
producer before PostgreSQL, removes its attestation, data, configuration,
installed script and units, and reloads systemd. A repeated removal is a no-op.
The locked OS identity, empty parent directories and the separately managed
backup enrollment remain; its uid is never recycled. A partially initialized
or foreign cluster is refused for administrator reconciliation instead of
being deleted speculatively. The live database, its roles, data and services
are never removal targets.
