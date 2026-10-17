# Versioned release environments (L232b)

`deploy/mise-a-jour/environnement-versionne.py` prepares a Git snapshot and
an independent Python environment in a private release store. It is a local
component for the host controller under construction; it does not run SSH,
restart services, migrate a database or validate a signed deployment decision.
The existing workstation/VM installers have not yet been wired to it.

`--store STORE prepare --repository REPO --manifest MANIFEST` requires:

```json
{
  "commit": "<full 40-character approved Git commit>",
  "python": "<absolute enrolled interpreter path>",
  "python_sha256": "<interpreter SHA-256>",
  "python_version": "<exact Python version>",
  "pip_version": "<exact pip version after installing pinned wheels>",
  "wheels": [
    {"path": "<absolute wheel path>", "sha256": "<wheel SHA-256>"}
  ]
}
```

The approved wheel set must cover all build and runtime dependencies,
including the project's build backend. Wheel files are copied into the
private release directory and hashed again before installation. Pip uses
`--no-index --no-deps`, ignores user pip configuration and inherited pip
options, and builds the source with `--no-build-isolation`. Missing dependencies
fail `pip check` or the import probe; no package index is consulted. The
manifest is an input from the enrolled controller, not itself an authorization
receipt. The controller must validate its approved digests and destinations.

The utility checks the interpreter executable's digest and runtime version;
this is not an inventory of its standard library or system shared libraries.
The host operation must pin an immutable, separately enrolled Python
toolchain. A venv alone cannot freeze updates to the host's Python runtime.
The helper itself requires Python with tar extraction filters (3.12 or later).

The release identity includes the full Git commit and manifest digest.
Preparation archives that exact commit, rejects links or unsafe archive entries,
and builds in the final directory so installed command shebangs remain valid.
It writes the completed receipt only after dependency/import checks and file
syncs. A failed preparation removes its incomplete directory and preserves
the current release. A process killed before cleanup leaves an incomplete
directory that is refused on retry; the controller must journal and reconcile
that staging artifact before rebuilding. Completed environments are retained.

`--store STORE activate --release ID --fenced` switches one atomic symlink,
`STORE/current`, so `current/source` and `current/venv` refer to the same
release. A private lock serializes preparation, activation and rollback.
`switch.json` records the previous and target releases before pointer
replacement; repeating an activation preserves the original rollback target.
`--store STORE rollback --fenced` restores the prior pointer without pip or
dependency changes, and refuses to undo an unrelated deployment.

`--fenced` is an assertion from the caller. Before using either switch command,
the controller must verify deployment authority, schema readiness, runner drain
and a claim fence. Host enrollment must make all consumers use the paired
current paths. Converting legacy real `venv` directories and `src-current`
links is a separate fenced enrollment step; this component does not move them.
Post-switch health checks and automatic rollback belong to the controller.

`tests.test_l232b_versioned_environment` checks exact commit extraction,
offline pip arguments, failed-build cleanup, incomplete/tampered releases,
missing fences, atomic pair switching, idempotence and crash reconciliation.
It also builds two real venvs with tiny synthetic offline wheels and proves
rollback restores both the older source and its older dependency version.
That test does not establish a complete production ameesh toolchain pilot.
