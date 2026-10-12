---
type: Study
title: "Project modules and structured lifecycle evidence"
description: "L317a: project-scoped extensions to work item types and lifecycle rules; IT execution evidence."
status: draft
tags: [modules, lifecycle, IT]
generated: { by: "ameesh-cx1", at: "2026-10-12" }
---

# Design for lot #318

The core supplies project-scoped module registration, structured module
milestones and lifecycle rules. It has no knowledge of releases, binaries,
publication destinations or execution proof. Installed modules are selected
by name from a fixed registry, never imported from canon-supplied Python paths.
`extensions.ameesh.projects.<project>.modules` in `federation.yaml` enables
them. Trusted canon synchronization persists their declarations in the same
transaction as the canon application; unreadable or refused canon cannot
disable an existing rule.

Generic lifecycle rules require an evidence milestone whose subject matches
the latest subject declaration for that work item. The database enforces
these rules for transitions and review milestones, covering CLI, automatic
merge synchronization and direct storage calls. Module milestones are
append-only and serialized on the work item row. The existing lifecycle is
unchanged for projects with no active module. Existing core work item types
remain valid; additional types come from the project's active modules.

The IT module owns `release` and `publication` types, covering tags, GitHub
releases, update manifests, stores and production deployments. Its `delivery`
milestone declares the delivered artifact's SHA-256; its `proof` milestone
records a real successful local execution, argv, bounded output excerpt and
the same SHA-256 checked before and after execution. A new delivery declaration
invalidates earlier evidence, including a return to a previous digest.
`merged`, `promoted` and a review `ok` require proof for that declaration.
Git commit SHA and artifact digest are separate identities.

Execution is explicit, bounded and never performed by a transition or by
canon synchronization. The module records observed evidence; it cannot prove
the quality of an operator-selected test or establish an OS-level attestation.
Audit reporting is a separate module rule in subsequent lot #319.

Validation covers project isolation, inactive modules, malformed canon,
missing/wrong/stale evidence, successful execution, failed and timed-out
execution, artifact changes, review GO, both terminal transitions and automatic
merge paths. Database checks run in CI on both supported drivers.

# Review r1 correction (12 October 2026, mesh-design mail 9262)

The initial payload validation did not establish execution provenance: an
executor could forge evidence or remove rules through its table grants.
Policy writes now require the isolated canon-sync role (or migration owner),
and the executor role contract revokes and audits policy DML. Evidence schemas
name an isolated writer role; the SQL milestone guard checks current database
role membership, never a caller-controlled GUC. The IT CLI submits execution
inputs to a fixed one-shot privileged helper; the helper uses a project-private
connection and drops artifact execution to an unprivileged UID with no inherited
groups or environment credentials. Approved interpreters come from protected
project configuration. Installation is described in deploy/it-proof/README.md.

Active projects take a shared project lock for lifecycle validation; policy
writes take its exclusive counterpart. Projects without active modules take
no project lock, and independent lots no longer serialize or deadlock merely
because they share a project. Declaration/evidence ordering remains protected
by the lot row lock.

# Review r2 correction (12 October 2026, mesh-design at 11:03)

Hashing the caller's path before and after execution permits A/B/A substitution.
The helper now opens the source with O_NOFOLLOW, streams it into a helper-owned
snapshot under a protected root-owned parent, marks the copy read/execute-only,
and hashes those exact copied bytes. It checks the delivery digest before
launching and executes only the snapshot. No subsequent read of the source
path participates in proof. The unprivileged artifact UID and executor cannot
modify or replace the snapshot. Evidence retains the source path, executed
snapshot path and argv; the copy is removed when execution finishes. Tests
replace A by B at Popen then restore A, assert A output, refuse symlinks and
check that a source mutation cannot alter the snapshot binding.

# Review r3 correction (12 October 2026, mesh-design mail 9548)

Unbounded copying of a huge sparse file could consume the helper's disk before
digest refusal. Project-private snapshot_limits now cap logical source size,
actual copied bytes, aggregate live/stale copies, minimum volume reserve and
copy time. The source descriptor's st_size is checked before target creation;
stream counting also rejects growth after that check. A store lock serializes
admission/copy, and the live snapshot remains counted after the lock is released
for execution. Volume space is checked before/after every block. Lock waits and
blocking copy I/O are bounded by a main-thread timer and monotonic checks;
copy consumes the requested execution timeout instead of preceding it. Error
cleanup removes partial copies. Tests use a small sparse file and reduced caps,
never allocate a huge file, and exercise growth, expiry, reserve and total budget.

# Design for lot #319

Installed modules may provide audit findings for their active projects. The
core collects these findings as generic `module_deviation` alerts, without
knowledge of release types, artifacts or execution. The IT module queries
release/publication lots and the latest delivery together in one database
snapshot. Missing delivery, missing proof, and proof bound to an older
declaration are deviations, including already delivered historical lots.
Abandoned and superseded lots are excluded. Proof for the current delivery
resolves the finding on the next pass. Auditing never executes an artifact.

Findings retain the lot, project, module, rule and current declaration ID.
The existing notifier sends them to the owner's configured alert channel
and briefs the project's orchestrators and explicitly declared auditors through event mail.
A project matches the registry's project identity, independently of the
assignee's team. Auditors require an `auditeur`/`auditor` canon role and must
belong to the project or have no project identity (global auditor). There is
no fallback to agents of other projects or to the implicit `auditeur` name;
without an eligible agent, the owner's alert channel receives the finding.
The alert's identity includes the declaration so a
new delivery starts a new episode rather than reusing a previous notice.
Orchestrators also require an explicit canon role, rechecked at delivery.
Recipient project checks hold a registry row share lock through mailbox commit,
on both database drivers. Event thread writes occur after commit in the finding's
project, never the recipient's team. Human routing follows the lot's work-package
owner or the configured default, never the assignee's human or host; persisted
retries/resolutions are reconciled with that routing.
No automatic decision request is needed: the owner's alert channel is the
authorized reporting route. A missing channel is reported by the notifier's
existing delivery diagnostics, never replaced by prose mail to the owner.

Tests cover inactive projects, both IT types, missing and stale proof,
redeclaration with identical bytes, resolved findings, historical terminal
lots, project-specific routing, notifier deduplication and human delivery.
Database integration is delegated to CI on both drivers; local checks stay
focused and offline under nice -n 10.
