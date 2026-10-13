# Release controller protocol (L232b)

`deploy/mise-a-jour/controleur-release.py` provides a durable ordering engine
for an **independent controller process**. It is not installed as a service,
contains no SSH/systemd implementation, and accepts no CLI request to deploy.
Authority and host effects currently exist only as injected interfaces tested
with simulated adapters. A boolean supplied by a runner is not an enrolled
implementation of either interface.

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
The next integration must supply signed authority, durable host receipts, the
cross-host database lock and bounded SSH/systemd adapters; helper/bootstrap and
installer wiring are also unfinished.
