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
- `TERMINAL`: states without further normal transitions. A recorded delivery
  can still be promoted, so it is completed without being terminal.

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
never counts. The test lists two legacy storage adapter exceptions in
`mesh_cli.py` and `work.py` until those interfaces use the registry. New imports
fail, and removing an adapter requires removing its exception from the test.
The blocking vocabulary boundary will be introduced with L98 through module
imports and the registry, rather than word counts. Architectural review remains
necessary.

L98 tranche 1 adds the `Module` interface (name, version, capabilities, CLI
and storage entry points) and `registry.discover()`, which reads the built-in
modules and the `ameesh.modules` entry points; `ameesh modules [--json]` lists
them read-only and exits 1 when an entry point is refused. The bare profile
(`discover(builtin=False)` with no entry point) is an empty registry without
error, pinned by `tests/test_l98_modules_registry.py`.

L99 tranche 1 adds the generic lifecycle names as accepted aliases:
`requested` (intake), `in_progress` (build), `to_validate` (qa), `validated`
(merged) and `delivered` (promoted); `blocked`, `waiting_human` and `closed`
keep the same spelling. `work_lifecycle.ALIASES` names every accepted spelling,
`canonical()` returns the persisted v1 one and `lifecycle_name()` the generic
one. `work move`, `work list --state` and the lots API filter accept either
spelling, and `work show` and `--json` carry a derived `lifecycle` field
beside `state`. Storage does not change: the v1 spellings stay persisted
until the storage switch.

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
work item type. It is always active, and the canon still cannot select it (a
descriptor without types is refused in a project policy) until L98 T2 defines
activation by federation or host. The core obtains the workspaces façade
through `registry.get("dev")`; its `audit` hook delegates the `canon check`
workspace findings (`host-work-dir-*`, `host-workspaces-unknown`) under the
Module interface. The bare profile has no `dev` module and documents
housekeeping and worktrees as no-ops. Installed entry points cannot
declare connectors, and a name declared twice or taken by the core is
refused. `registry.merge_connector()` replaces
`work_lifecycle.delivery_connector()`. The lot journal names the connector
(`[via <connector>]`); storage is unchanged. Tranche 2b (#4366)
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
aliases are the only new direct module imports outside `modules/`, declared as
exceptions in `tests/test_generic_core.py` until L104 removes them.

The boundary check preserves the current runtime behavior. Module activation,
renamed persisted states, validation as a stored state, extraction of development
and forge tooling, vocabulary selection and declarative pipelines are subsequent
parts of the generic-core transition.
