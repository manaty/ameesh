# Core and modules

The **core** coordinates work items, communication, governance, resources and
agent execution. A **module** supplies domain vocabulary, work item types,
rules and commands. The core reaches these behaviors through the module
registry; it does not import a business module's implementation directly.

| Owned by the core | Supplied by modules |
|---|---|
| Work tracking and generic lifecycle operations | Domain types, schemas and lifecycle rules |
| Mail, readable threads and human decisions | Domain commands and vocabulary |
| Identity, leases, budgets and agent execution | Audits and tooling exposed through declared capabilities |

Git used to read the canon or preserve identity is core infrastructure. Git
branches and pull requests as work products belong to the development module.
See also [the canon](canon.md), [runner and leases](runner-and-leases.md) and
[actions and the gate](actions-and-gate.md).

## Discovering installed modules

```bash
ameesh modules --json
```

This read-only command lists discovered modules. Discovery and activation are
separate: installing a module makes its implementation available; the canon
selects its policy for projects.

The Python interface `ameesh.modules.registry.Module` describes each discovered
module:

| Field | Meaning |
|---|---|
| `name` | Module name |
| `version` | ameesh version for built-ins, distribution version for external modules |
| `source` | `builtin` or `external` |
| `capabilities` | Supported interfaces: `lifecycle`, `cli`, `audit`, `workspaces` |
| `cli` | Command entry point, when provided |
| `storage.types` | Work item types supplied by the module |
| `descriptor_sha256` | Fingerprint of the compiled descriptor |
| `distribution`, `entry_point` | Installation provenance for an external module |

`registry.discover()` returns the discovered modules and refused entries. A
refused entry is reported with its reason and does not become a module;
`ameesh modules` exits 1 when it reports such an entry.

External Python packages register an entry point in the `ameesh.modules`
group. Its name identifies the module; its target supplies `NAME` and
`descriptor(options)`. The descriptor declares types, schemas and rules.
Installed modules execute inside the ameesh process: installation requires
trust in the package and its revision. They are not sandboxed. See the
[security model](../security.md).

## Activating a project policy

Declare modules in the trusted canon's `federation.yaml`:

```yaml
extensions:
  ameesh:
    modules: [it]
    teams:
      ops:
        modules: {it: {require_qa: [promoted]}}
    projects:
      service:
        modules: {it: {require_qa: [merged, promoted]}}
```

The federation selection applies to its known projects. A team selection
applies to that team's projects, and a project selection applies only to that
project. For a module named at several scopes, the narrowest scope supplies
its options: project overrides team, which overrides federation. Lists select
modules with default options; mappings supply options per module.

`ameesh canon check` validates the declarations. `ameesh canon sync` applies
them through the normal [trusted canon procedure](canon.md#only-the-approved-source).
Two modules cannot claim the same work item type in one project. An unknown
selection or an activated but refused entry point blocks sync; a refused entry
point that no scope activates produces a warning.

```bash
ameesh modules --active --json
ameesh doctor
```

`--active` lists applied project policies, including their canon, scope,
project, module, version and descriptor fingerprint. Without a reachable
database it compiles the local canon instead. `doctor` compares applied
policies with the readable canon and installed descriptors: a missing active
module is an error; a changed descriptor or an unapplied declaration requires
another sync. Discovery alone does not prove that a policy was applied.

Removing a declaration does not erase historical milestones. Keep a module
available while its work items remain active: its additional types fail closed
without their policy.

## Built-in examples and the bare profile

**`dev`** supplies development tooling. It has no work item type and is always
active in the standard profile; it cannot be selected or disabled through a
project's module declaration. Its `workspaces` interface supplies working
directories and housekeeping, and its `audit` hook checks host workspace
settings. Its commands include:

```bash
ameesh module dev review-class --help
ameesh module dev sync-merges --help
```

The existing `ameesh review-class` and `ameesh work sync-merges` commands are
retained aliases. Development work items are called **lots**; the core uses
**work item**.

**`it`** supplies IT work item types and lifecycle evidence rules. Its
`require_qa` option in the example above requires QA evidence for the named
persisted states (`merged` and `promoted`). Policy is selected per project;
its presence in the discovered list does not activate it everywhere.

A **bare profile** has no built-in modules and no installed entry points.
`registry.discover(builtin=False)` then returns an empty registry without
error. Without `dev`, workspace housekeeping and worktree operations are
no-ops. This is distinct from a standard installation with no project module
selection, where the built-in development tooling remains available.

The repository's `tests/test_generic_core.py` checks the import boundary:
new direct imports of module implementations from the core are refused.
Declared compatibility exceptions remain explicit; the registry is the
supported interface. The [architecture document](../design.md) explains this
boundary in more detail.
