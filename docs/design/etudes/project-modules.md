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
