# Work lifecycle

A **work item** is the core's unit of work. The development module calls it a
**lot** and adds its review, integration and deployment workflow. These are
two vocabularies for the same work item and identifier.

## Core states and accepted names

The generic lifecycle is:

```text
requested → in_progress → to_validate → validated → delivered
```

This is a reading guide, not permission to skip the workflow's guards.
`blocked` and `waiting_human` record waits; `closed` records closure.
The active workflow controls which transitions are allowed.

| Generic name | Stored core state | Meaning |
|---|---|---|
| `requested` | `intake` | Work has been requested |
| `in_progress` | `build` | Work is being carried out |
| `to_validate` | `qa` | Work awaits validation |
| `validated` | `validated` | Validation has been recorded; delivery is still due |
| `delivered` | `delivered` | Delivery has been recorded |
| `blocked` | `blocked` | Progress awaits a prerequisite |
| `waiting_human` | `waiting_human` | Progress awaits a human decision |
| `closed` | `closed` | The work item has been closed |

Both generic and compatibility spellings remain accepted by state arguments
such as `work list --state` and `work move`. In Python,
`work_lifecycle.canonical()` returns the current stored spelling;
`work_lifecycle.lifecycle_name()` returns the generic name. An unknown name
is rejected. The derived `lifecycle` field exposes the generic name in work
item output.

## Development stages

The `dev` module distinguishes **integration** (`merged`) from **deployment**
(`promoted`). Both persist the core state `delivered`; the workflow stage and
`legacy_state` preserve their development meaning. A recorded merge does
not prove that a deployment happened. `validated` remains a separate stored
state and does not mean merged or deployed.

`work_lifecycle.phase()` reads the compatibility phase. Existing development
commands and output can therefore still say `merged` and `promoted`.
Historical events keep their original spellings. Compatibility commands also
retain their meaning: in `dev`, `work move <id> delivered` requests promotion,
while `work move <id> merged` records the integration stage, subject to its
guards. Do not infer the development stage from the stored core alone.

## Delivery connectors

The Python entry point `work.deliver(db, id, connector=…, evidence=…)` routes
delivery through a connector and records its name in the work item's journal.
It is a Python API, not an additional `ameesh work deliver` shell command.

| Connector | Evidence | Existing command |
|---|---|---|
| `git-merge` (`dev`) | Merge commit and pull request, or a confirmed merge action | `ameesh work merged` |
| `document` (core) | Reviewed commit SHA for delivery outside the target branch | `ameesh work close <id> --delivered --sha <reviewed-sha>` |
| `done` (core) | No merge evidence; the work itself is complete | `ameesh work close <id> --done` |

The `document` and `done` paths store `closed`, with a close reason that
distinguishes completed work from abandonment. Read that reason alongside
the state when interpreting a closure.

The module registry supplies module connectors. Each connector checks its
own evidence and uses the corresponding delivery path; the generic name
does not bypass review or deployment requirements.

## Display vocabulary and compatibility

The core vocabulary (`vocab.CORE`) calls a work item a *tâche* (task).
The development module declares *lot*. `vocab.active()` selects a vocabulary
from discovered, always-active module implementations; when none declares one, it falls
back to `CORE`. Installing a module alone does not make its vocabulary the
active vocabulary. The English documentation uses **work item** for the
core and **lot** for development work.

Display vocabulary does not rename commands, options such as `--lot` and
`--new-lot`, JSON keys such as `lots[]`, database names such as `work_items`,
configuration keys, thread filenames, or identifiers. Compatibility aliases
are retained without a removal date.

See [Core and modules](core-and-modules.md),
[Sessions](sessions.md), [Readable threads](threads.md), and the
[Progress view](../guides/progress-view.md) for how work is presented.
