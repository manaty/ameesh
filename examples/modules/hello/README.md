# Hello module

This package registers `hello` in the `ameesh.modules` entry-point group. It
admits `hello-task` work items in projects that select it and provides a
read-only greeting command. It accepts no options and installs no database
schema or lifecycle evidence rules.

From the repository root, install into a disposable environment (never the
shared runner environment):

```sh
python3 -m venv "$TMPDIR/ameesh-hello-venv"
"$TMPDIR/ameesh-hello-venv/bin/pip" install . ./examples/modules/hello
"$TMPDIR/ameesh-hello-venv/bin/ameesh" modules --no-canon --json
"$TMPDIR/ameesh-hello-venv/bin/ameesh" module hello --help
```

Set `TMPDIR` to a private temporary directory first if your shell does not
provide one. With a configured database and bound session identity, run
`ameesh module hello --name Alice` using that environment's executable.
The normal module dispatcher connects to the database and resolves the actor;
help works without a database. The command only prints a greeting, and does
not itself check project activation. Any future command that writes work items
must validate the target project and type through the normal core APIs.

In a trusted canon, select the module for a known project:

```yaml
extensions:
  ameesh:
    projects:
      demo:
        modules: [hello]
```

Run `ameesh canon check`, apply through the normal trusted `ameesh canon sync`
procedure, then inspect `ameesh modules --active --json` and `ameesh doctor`.
Installation alone does not admit the type. Complete or migrate active work
items before removing the declaration, syncing, and uninstalling the package.
Installed modules execute trusted Python code in the ameesh process.

The author guide is [Write a module](../../../site/docs/guides/write-a-module.md).
The focused contract test is `tests.test_l104_module_example`.
