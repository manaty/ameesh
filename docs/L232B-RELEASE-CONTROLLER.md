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
The next integration must enroll authority and supply durable host receipts, the
bounded SSH/systemd adapters; helper/bootstrap and
installer wiring are also unfinished.

## Scoped signed decision adapter

`autorite-release.py.question(operation, manifest, expires_at=EPOCH)` prepares a
reviewable question containing the exact manifest, its digest, host, operation,
allowed release/recovery steps and execution deadline. Use that exact question
and the module's `OPTIONS` with `decide ask --needs-signature` only when the
host operation is fully prepared. This development checkpoint sends no request.
The expiry must cover the rollback window. A prepared question is not authority.

`SignedDecisionAuthority(db, decision_id, operation, manifest)` implements the
controller's validation interface. Only an exact signed `apply` answer counts;
a free-text answer, decline, pending decision, different operation, changed
manifest or unknown action does not. It re-reads the decision and runs existing
Ed25519 proof verification against the currently enrolled owner key. Revocation,
key replacement, a changed signed question and corrupted signatures invalidate
all subsequent release and recovery steps. The approval nonce must have been
consumed by this exact decision; this is not a new unconsumed approval.

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

`helper-redemarrage.py.RestartHelper` supplies system-mode restart and recovery
restart, restricted to exact enrolled `ameesh-runner@…` or
`ameesh-runner-agent@…` instances. Its policy identifies the host, allowlist,
independent controller service and 5–300 second restart budget. The signed
manifest's policy reference must be `policy-` plus the canonical policy digest,
so changing that allowlist or budget invalidates operation authority. The
controller service itself is outside the allowlist and `system.slice` holds
the action workers independently of runner slices.

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
they do not prove a real host restart. The bootstrap, pinned helper runtime,
restricted SSH/sudo enrollment, full independent-service adapter, config/caps/
guard snapshots, installer wiring and authenticated health/rollback pilot are
still required. This checkpoint implements neither user-mode systemd nor other
root operations and grants no root authority to an agent.

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
never a transport-selected path. `LinuxHost` requires root, checks machine-id and
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
invocation and verified read-only runtime/source trees. The service coordinator
and fixed unit are now prepared as described below. **The complete host adapter
factory remains unfinished**, so the real backend holds before activation.
There is no installed bootstrap CLI, automatic
root executor or runtime/credential enrollment. No real root/account/SSH/systemd
effect is executed by this work. Tests combine disposable filesystem copies with
simulated accounts/services and synthetic real signatures on disposable PostgreSQL
through both drivers. Authenticated host pilot and external toolchain admission
remain required.

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
not a launched system service or authenticated host pilot. **The pinned concrete
`adaptateur-hote-release.py` factory is not supplied yet**; real startup therefore
fails before DB connection/readiness instead of admitting simulated backends.
Full host adapters, health probe, release configuration/caps/guard snapshots and
installer/provider wiring remain build work.

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
