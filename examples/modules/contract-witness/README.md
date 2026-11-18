# Contract witness module

`ameesh-contract-witness` is the external consumer that ameesh's CI uses to
qualify the [external module contract v1](../../../docs/api/module-contract-v1.md).
It is its own distribution, outside `src/`, and uses only the published
contract:

- `ameesh_contract_witness.py`, the installed module: `NAME`, `descriptor`,
  `CAPABILITIES`, `main(db, argv, *, actor)`, `audit(store, project)` and the
  named `ModuleStore` operations. It never queries `db`.
- `contract_witness_client.py`, the client: installed executables as
  subprocesses, the HTTP discovery and OpenAPI documents, and explicit
  columns of the named SQL views. Standard library only.

Neither file imports ameesh or `tests`; `tests/test_7665_module_contract.py`
checks that, installs the distribution metadata the way pip would and runs
each contract element against the live code. The element → test table is
[module-contract-v1-coverage.md](../../../docs/api/module-contract-v1-coverage.md).

To try it by hand in a disposable environment (never the shared runner
environment):

```sh
python3 -m venv "$TMPDIR/witness-venv"
"$TMPDIR/witness-venv/bin/pip" install . ./examples/modules/contract-witness
"$TMPDIR/witness-venv/bin/ameesh" modules --no-canon --json
"$TMPDIR/witness-venv/bin/ameesh" module contract-witness --help
```
