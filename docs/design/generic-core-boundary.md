# Generic core boundary

The generic core owns work tracking, communication, governance, resources and
agent execution. Business modules own their domain vocabulary and behavior.
Git used for the canon, identity and persona memory remains infrastructure;
git branches and pull requests as work products belong to development tooling.

`work_lifecycle` centralizes the current persisted lifecycle spellings. `work`
re-exports its public state groups.
Readers should use these groups rather than repeat storage spellings:

- `ACTIVE`: requested work and work in progress.
- `WAITING`: blocked work and work waiting for a human decision.
- `TO_VALIDATE`: work awaiting validation.
- `DELIVERED`: delivery recorded or promoted.
- `COMPLETED`: delivered work and explicitly closed work.
- `TERMINAL`: completed core outcomes (`delivered`, `closed`), including the
  historical promotion spelling for compatibility. Development transitions
  inside a delivered core are governed by its module stage.

The legacy `closed` state also covers delivery outside the target and completed
review missions. Its close reason must be checked before treating it as an
abandonment. Public presentation states, action states, milestone kinds and
remote issue states have separate contracts; their equal spelling does not
make them interchangeable with work-item states.

`tests/test_generic_core.py` scans Python literals and identifiers outside
`modules/`, plus `progress_page.html`. Python docstrings and comments are
excluded. Run `PYTHONPATH=src python -m tests.test_generic_core --report` to
print current counts per file and motif. This lexical report is non-blocking;
concurrent changes to vocabulary do not require updating an exact baseline.
The scanners and their unit tests remain useful for inspecting the transition.

Only direct imports of module implementations are blocking, including relative
and aliased imports. The registry (`ameesh.modules.registry`, L98) is the
core's interface to modules and is not a module implementation: importing it
never counts. L98 T3 (#4406) removed the storage adapter exceptions of
`mesh_cli.py` and `work.py`. The only remaining direct imports are the pure
`sys.modules` aliases listed in `COMPATIBILITY_ALIASES`, each with its origin
lot, date and reason (L104 T6, #4414): this declared list counts as
infrastructure. New imports fail, and an entry whose import has gone must leave
the list. Removing the aliases is a separate lot (L104b); `ameesh work merge-pr`
and `ameesh review-class` are retained, as the canon's process cites them.
The blocking boundary is module imports and the registry, not word counts.
Architectural review remains necessary.

L98 tranche 1 adds the `Module` interface (name, version, capabilities, CLI
and storage entry points) and `registry.discover()`, which reads the built-in
modules and the `ameesh.modules` entry points; `ameesh modules [--json]` lists
them read-only and exits 1 when an entry point is refused. The bare profile
(`discover(builtin=False)` with no entry point) is an empty registry without
error, pinned by `tests/test_l98_modules_registry.py`.

L103 tranche 2 (#4396) adds a third source to the registry: with a canon,
`registry.discover(canon=…)` turns each valid `Pipeline` card into a `Module`
of source `canon` (`card_ref` for the trace, capability `lifecycle`, no stored
type). Its `PipelineModule` adapter lives in the registry and is built from
canon data: nothing is imported. `modules.installed(canon)` and
`modules.declarations(federation, canon)` accept the card's name as a project
module; a declarative module is the one module without types a project may
select (an installed one, and the always-active `dev`, still may not). A card
never shadows an installed name.

L103 tranches 3 and 4 (#4397, #4398) make that module executable and document
it. `ameesh module <name> adopt <lot>` writes the three pin columns under the
lot lock and one journal line, with no receipt and no migration: the card is
re-read from the exact pinned git object (`pipeline_at`, through the hardened
`canon.git_read`), never from a working tree or a branch. `advance`, `hold` and
`release` go through the native registry, so installed verifiers, optimistic
versioning and receipts are the same as for a Process admission. A pinned lot
is no longer movable by `work.move`; later revisions stay on the #4007 transfer
path. The consumer projection is the pure `process_projection.project_issue`
fed by the admitted artifact: no `plan_github` call is involved, and the core
writes no issue. The two shipped examples (`examples/pipelines/acme-work-delivery.md`
and `support-work-delivery.md`) are data, not code; their end-to-end proof is
`tests/test_l103_pipeline_examples.py`. The author guide is
[Write a Pipeline](../../site/docs/guides/write-a-pipeline.md).

The generic lifecycle is requested → in_progress → to_validate → validated →
delivered. The first three names accept the stored spellings `intake`, `build`
and `qa`; `validated` remains distinct from delivery. `blocked`, `waiting_human`
and `closed` retain their spelling. `work_lifecycle.ALIASES` lists accepted
names, `canonical()` returns the stored core and `lifecycle_name()` the generic
name. State arguments accept both vocabularies; work output adds `lifecycle`.
Development stages `merged` and `promoted` share the stored core `delivered`,
with `workflow_stage` and `legacy_state` preserving their meaning (details below).

The core vocabulary (`vocab.CORE`) names a work item “tâche”; the development
module declares “lot”. `vocab.active()` reads the discovered, always-active
module implementations and falls back to CORE. Display vocabulary leaves
commands, option names, JSON keys and identifiers unchanged. The English site
uses “work item” for the core and “lot” for development; see
[Work lifecycle](../../site/docs/concepts/work-lifecycle.md).

L99 tranche 2 (#4350) makes `work.deliver(db, id, connector=…, evidence=…,
actor=…)` the single delivery verb. The core owns two connectors: `document`
(delivered outside the target, evidence `{sha}` of the reviewed head, behind
`work close --delivered`) and `done` (delivered without merge, no evidence,
behind `work close --done`). `git-merge` is declared by the built-in `dev`
tooling provider in `DELIVERY_CONNECTORS` and read through
`registry.delivery_connectors()`; its evidence is either `{sha}`/`{pr}` (a
declared merge, `work merged`) or `{action}` (a merge confirmed by the gate).
L101 tranche 4 (#4357) integrates `dev` on the Module interface: `discover()`
lists it as a built-in tooling module with the `workspaces` capability and no
work item type. It is always active, and the canon cannot select it (a
descriptor without types is refused in a project policy). Project policy
activation applies to modules with work item types. The core obtains the
workspaces façade
through `registry.get("dev")`; its `audit` hook delegates the `canon check`
workspace findings (`host-work-dir-*`, `host-workspaces-unknown`) under the
Module interface. Those settings are host-scoped, so `modules.audit` runs the
hook once, with `project=None`, after the project modules: one finding is not
repeated per project and carries its host and agent. It has no project
orchestrator or project event brief; the human route uses the responsible of
its agent, then of its host, then `notify.default_human`, and two hosts with
the same rule stay two alerts. The bare profile has no
`dev` module and documents housekeeping and worktrees as no-ops. Installed
entry points cannot declare connectors, and a name declared twice or taken by
the core is refused. `registry.merge_connector()` replaces
`work_lifecycle.delivery_connector()`. The lot journal names the connector
(`[via <connector>]`). Tranche 2b (#4366)
routes the merges observed by the sweeps and the packet and merge-queue paths
(`plan_git`, `plan_github`, `packet_close`, `merge_pr`, `fire`) through the
same verb, with a third `git-merge` evidence `{seen: <source>, sha, pr}`; a
test refuses any new direct call to `work.close_merged` outside the
`git-merge` connector and `work.record_merge`.

L100 tranche 1 (#4382) moves the development merge paths into the built-in
`dev` provider. The `git-merge` action connector lives in
`modules/dev/git_merge.py`; `connectors.get("git-merge")` reads it through
`registry.action_connectors()`, which follows the delivery rule: the core
keeps `shell-noop`, a name declared twice or taken by the core is refused, and
installed entry points cannot declare action connectors. The `work merge-pr`
gesture lives in `modules/dev/merge_pr.py`; the `ameesh work merge-pr` route is
unchanged. The old paths `ameesh.connectors.git_merge` and `ameesh.merge_pr`
are `sys.modules` aliases of the same module object, so importers and tests
patching a module global by the old name keep working;
`tests/test_l100_dev_aliases.py` pins identity and patching for both. Those two
aliases, like those of tranches 2 to 4, are declared in
`COMPATIBILITY_ALIASES` in `tests/test_generic_core.py`.

Installed module activation is compiled from `extensions.ameesh.modules`,
`teams.<team>.modules` and `projects.<project>.modules` in the trusted canon.
The narrowest scope that names a module supplies its options; project wins over
team and federation. `canon sync` applies these policies to `project_modules`;
`ameesh modules --active` reads the applied rows (or compiles the local canon
when no database is reachable), while `doctor` compares declarations, installed
descriptors and applied policies. Discovery remains separate from activation.
The standard profile includes the always-active `dev` tooling provider, whose
CLI is available through `ameesh module dev`; existing command aliases are
retained. The bare profile has neither built-ins nor external entry points,
and workspace operations without `dev` are no-ops.

The site's [Core and modules](../../site/docs/concepts/core-and-modules.md)
page describes this installed-module architecture. A new canon starts without
any project module: `tests/test_l104_bare_canon.py` runs `canon check`, `canon
sync` and a core work item through the CLI, in the standard and bare profiles,
and leaves `project_modules` empty.

L99 tranche 5 (#4353, owner decision #164165, signed approval #167) stores
both development delivery phases under `state = delivered`. Unpinned dev
lots use the existing `workflow_stage` for `merged` or `promoted`;
`legacy_state` carries the compatibility spelling. Pinned Process lots keep
their admitted revision and arbitrary stage names: signed artifacts and
receipts are not rewritten. Old Process cores remain interpreted under
their original terminal and projection rules.

The dev module declares these two stages and their compatibility transition
maps through `registry.development_workflow()`. A core alone does not tell
whether a development lot can still be promoted: use `work_lifecycle.phase`
for that distinction. New core filters select both stages; the old `merged`
and `promoted` filters continue selecting their individual stages. Public
GitHub labels and closure text retain their phase-specific wording.
`work move <id> delivered` keeps its pre-switch meaning: it requests
**promotion** (`work_lifecycle.phase_name` names that stage), it is not a
third stage; `--state delivered` (and the API filter) selects both `merged`
and `promoted`.

Migration 0518 is explicitly non-additive. Stop executors before applying it
and restart them after commit, as required by the signed decision. Do not
roll back the code against the switched database alone: the old executor
cannot read the new core. The disposable Postgres witness reconstructs the
pre-switch constraints/functions, applies the migration in a transaction,
rolls it back and verifies the original rows and milestones, then applies it
again. Historical events stay intact; the migration adds no synthetic
milestone or process receipt.

The explicit operator rollback is
`src/ameesh/migrations/rollback/0518_generic_delivery_state.sql`; it is outside
automatic migration discovery. With executors stopped, execute its SQL in one
transaction before restoring the previous code. It restores stage spellings,
constraints, policy and milestone triggers, and the fingerprint index. Tests
exercise this script and the forward migration on both delivery stages.
`work promote <id>` delegates to the dev module; the old
`work move <id> promoted` command remains accepted.

Schedule the dedicated delivery package and schema application in the same
staging window: a pending non-additive migration stops automatic packet
composition. Stop all executors before applying 0518, then start only the
new code. During that window, disable automatic code fallback or couple it
to the SQL rollback above; reverting code alone is unsafe. An offline host
must receive the new code before any of its executors starts against the
switched database.
