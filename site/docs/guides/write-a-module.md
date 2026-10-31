# Write a module

Choose an **installed Python module** when you need a domain work item type,
evidence policy or command. Choose a **declarative module** when you only need
a workflow: follow [Write a Pipeline](write-a-pipeline.md), which covers canon
cards, activation and lifecycle commands without Python code. The
[core and modules](../concepts/core-and-modules.md) page explains the boundary.

## 1. Package an installed module

Start from the executable
[hello example](https://github.com/manaty/ameesh/tree/main/examples/modules/hello).
It contains `pyproject.toml`, `hello_module.py` and installation instructions.
The distribution name can differ from the module name:

```toml
[project]
name = "ameesh-hello-module"
version = "0.1.0"

[project.entry-points."ameesh.modules"]
hello = "hello_module"

[tool.setuptools]
py-modules = ["hello_module"]
```

Use the example's complete build metadata when copying this fragment. The
entry point targets a module or object with `NAME = "hello"` and a callable
`descriptor(options)`. Entry-point names use letters, digits, `.`, `_`, `-`,
start with a letter or digit and have at most 64 characters. A name cannot
collide with a built-in, another installed entry point or a Pipeline card.
The canon selects installed names; it never supplies Python import paths.

```python
NAME = "hello"
TYPES = ("hello-task",)
CAPABILITIES = ("lifecycle", "cli")


def descriptor(options=None):
    options = dict(options or {})
    if options:
        raise ValueError("unknown hello option(s): %s" % ", ".join(sorted(options)))
    return {"types": list(TYPES), "schemas": {}, "rules": []}
```

Discovery calls `descriptor({})`, so defaults must work. A project selection
compiles it again with that project's options. Reject unknown or malformed
options explicitly and return JSON-serializable data with exactly these keys:

| Key | Contract |
|---|---|
| `types` | Non-empty list of unique names, using the module-name grammar |
| `schemas` | Mapping of milestone kinds to their evidence schemas |
| `rules` | List of mappings defining lifecycle evidence requirements |

An external module must declare at least one type. Optional `TYPES` must match
the compiled list, including order, for every option set; omit it if types
vary with options. The hello example intentionally has no evidence schema or
rule. For a policy with evidence, study the built-in
[`it` descriptor](https://github.com/manaty/ameesh/blob/main/src/ameesh/modules/it/__init__.py)
and its tested milestone schemas, subject bindings, state requirements and
separation of duties. Use `ModuleStore` and core work APIs rather than writing
policy rows or bypassing lifecycle guards directly.

## 2. Provide commands and audits

`CAPABILITIES` may contain unique names from `lifecycle`, `cli`, `audit`,
`workspaces`. Lifecycle is available through the descriptor; other declared
capabilities need corresponding callable hooks. If omitted, capabilities are
inferred from the implementation. `workspaces` is the development tooling
interface; an ordinary domain module does not need it.

A CLI hook has this signature and returns an exit code:

```python
def main(db, argv, *, actor):
    # Parse argv, use db and the resolved actor for authorized core operations.
    print("Hello, world!")
    return 0
```

Expose it with `ameesh module hello …`. The dispatcher supplies a database
connection and resolved actor; `--help` receives `db=None` and `actor=""`, so
parse help before using them. Installation exposes the command even without
project activation. A command operating on a work item must check its project,
active module and admitted type, and preserve core authorization rules.

An optional `audit(store, project)` returns a list of finding dictionaries. It
runs for each applied project selecting the module. Follow the built-in `it`
audit's finding format (`type: module_deviation`, `module`, `rule`,
`project`, `lot`, `reason`, `detail`, `action`, and the relevant evidence facts) and keep the hook read-only. Declare `audit` only when providing
that callable; the hello example has no audit hook.

## 3. Install, discover and trust

Install the package into the **same Python environment as ameesh**, on every
host that needs to read its policy or execute its commands. Use a disposable
environment for development; the example README gives complete commands.
Pin and review the package revision before operational installation.

```sh
ameesh modules --no-canon --json
ameesh module --list
ameesh module hello --help
```

`ameesh modules` lists discovered built-ins, installed entry points and readable
Pipeline cards, with name, version, source and capabilities. `--json` adds the
distribution, entry point and descriptor fingerprint of each module.
`--no-canon` lists built-ins and installed entry points, excluding Pipeline
cards from the canon. `module --list`
reports installation evidence, including per-entry `ok` and errors; inspect
these fields rather than treating its exit code as a validation result.

Installed modules execute inside the ameesh process, **without a sandbox**.
Import, descriptor compilation, commands and audits are trusted code; a
capability declaration is not a permission boundary. See the
[security model](../security.md).

## 4. Activate the policy and check it

Select the module for an existing project in trusted `federation.yaml`:

```yaml
extensions:
  ameesh:
    projects:
      demo:
        modules: [hello]
```

A list selects defaults. A mapping supplies options, for example
`modules: {hello: {}}`; this example rejects all non-empty option mappings.
Federation and team scopes can also select modules; project options override
team options, which override federation options for the same module. See
[activation scopes](../concepts/core-and-modules.md#activating-a-project-policy).

```sh
ameesh canon check
ameesh canon sync
ameesh modules --active --json
ameesh doctor
```

Sync follows the [trusted canon procedure](../concepts/canon.md#only-the-approved-source).
Discovery is not activation: only applied project policies admit their types.
Two selected modules cannot own the same type in one project. `bug`, `evolution`
and `improvement` remain core types; custom types need their active module.

Discovery reports refused entries and `ameesh modules` exits 1 if any are
refused. Invalid names, duplicate entries, mismatched `NAME`/`TYPES`, missing
hooks and bad descriptors are refusals. An unknown selection or an activated
refused module blocks canon sync; an unselected refused entry produces a
warning during policy resolution. The database guard fails closed for a type
without an active policy. `doctor` reports missing active modules and drift
between installed descriptors, canon declarations and applied rows.

## 5. Test and maintain the module

Run the example's focused checks from a source checkout:

```sh
PYTHONPATH=src python3 -m unittest tests.test_l104_module_example
PYTHONPATH=src python3 -m unittest tests.test_l317_project_modules tests.test_l98_modules_registry
```

The hello test exercises real metadata discovery and loading from the package's
entry-point declaration, command output, capability reporting and option
refusal. The existing
[external fixture](https://github.com/manaty/ameesh/tree/main/tests/fixtures/ameesh_modules)
and
[project-policy tests](https://github.com/manaty/ameesh/blob/main/tests/test_l317_project_modules.py)
show canon admission, descriptor validation and PostgreSQL guards. Add tests
for your own schemas, evidence requirements and project options, including
negative cases and actor checks for commands that write.

For upgrades, install the reviewed revision, check the canon, sync changed
policies and inspect `doctor` and the applied fingerprint. Preserve compatibility
with historical evidence. For removal, finish or explicitly migrate active
work items first, remove the canon selection and sync, then uninstall the
package from its hosts. Removing a declaration does not erase historical
milestones; removing a policy while its custom work items are active can make
them fail closed.
