# Installing project IT proof production

The producer is a one-shot helper, not a daemon. Installation belongs to the
host/database operator. Nothing in canon sync or a release transition installs
it or executes an artifact.

1. Apply migration 0363 with the migration owner. Apply
   `deploy/sql/role-module-writers.sql`, then reapply
   `deploy/sql/role-executor.sql`. The executor receives SELECT only on
   `project_modules`; effective inherited policy writes make its role audit
   fail. Its direct milestone INSERT remains available for delivery, but
   evidence INSERT requires the isolated writer role in the milestone schema.
2. Create separate nonadministrative LOGIN roles for canon synchronization and
   IT proof, members respectively of `ameesh_canon_sync` and `ameesh_it_proof`.
   Never grant either writer group to an executor login. Keep passwords in the
   services' protected configuration or pgpass files. Run `ameesh canon sync`
   under a separate OS account with the canon-sync login and protected code,
   configuration and canonical checkout. The existing trust checks and atomic
   registry/module-policy transaction still apply; an executor attempting a
   policy write is refused, even if an old ACL grants it that write.
   After creating the logins and memberships, reapply
   `deploy/sql/role-module-writers.sql`: it rejects TEMP on the writer groups,
   their transitive members and those members' parent roles, including PUBLIC
   and SET ROLE paths. Reapply it after membership or database ACL changes;
   a failed audit does not revoke the offending grants. See the
   [TEMP barrier for module guards](../../docs/EXPLOITATION.md#isolated-it-proof-and-policy-writers-l317a-r2).
3. Install the reviewed ameesh wheel and its dependencies into the root-owned
   virtualenv `/opt/ameesh-it-proof`. Install the fixed wrapper
   `deploy/it-proof/ameesh-it-proof` as
   `/usr/local/libexec/ameesh-it-proof`, owned by root, mode 0755. The virtualenv,
   wrapper and all parent directories must be unmodifiable by agents. The
   wrapper invokes Python with `-I`, ignoring agent Python paths and startup
   configuration, and accepts no command-line arguments.
4. Create an unprivileged artifact worker account, for example `ameesh-it-run`,
   without database memberships, access to service credentials or access to
   root's files. Allow it to read/execute the artifacts to test. The helper runs
   as root solely to open its private configuration and drop the artifact
   child's UID/GID, supplementary groups and environment before execution.
   No artifact or runner is executed as root.
5. For each enabled project, create `/etc/ameesh-it-proof/<project>.json`, owned
   by root, mode 0600, under a root-owned directory not writable by agents.
   Example (fill the service connection using the operator's secret mechanism):

   ```json
   {
     "dsn": "postgresql://it_proof_service@127.0.0.1/mesh",
     "schema": "ameesh",
     "driver": "psycopg",
     "execution_user": "ameesh-it-run",
     "snapshot_root": "/var/lib/ameesh-it-proof",
     "snapshot_limits": {
       "max_bytes": 134217728,
       "total_bytes": 536870912,
       "min_free_bytes": 536870912,
       "copy_seconds": 15
     },
     "runners": ["/usr/bin/python3"]
   }
   ```

   Root's pgpass can hold the password. Approved runners and their dependencies
   must be trusted; a caller cannot choose `/bin/true` as a fake interpreter.
   Create the root-owned `snapshot_root` on disk, mode 0755, with all parent
   directories root-owned and unmodifiable by agents. Never place it in an
   executor-owned TMPDIR. Each request streams the artifact from an O_NOFOLLOW
   descriptor opened with the worker’s read permissions into a helper-owned copy,
   makes that copy mode 0555, hashes it,
   checks the declared digest and executes only the copy. The helper never
   rereads the original path after copying. It deletes the snapshot at exit;
   proof keeps its digest and actual command. Source substitution cannot change
   the executed bytes. Artifact-relative dependencies must be bundled or passed
   explicitly; a sibling directory of the original artifact is not copied.
   Limits are private project settings, never request parameters. Defaults are
   128 MiB per artifact, 512 MiB for all live/stale snapshots in this store,
   512 MiB free on its volume, and 15 seconds to admit/copy. The requested
   execution timeout also bounds the copy; its remaining time is used for the
   process. The helper checks descriptor `st_size` before allocating a copy,
   counts bytes to detect growing files, and checks free space before/after
   every block. The main-thread timer interrupts blocking copy I/O. Copy-lock
   waits consume the same deadline. Copies are serialized for admission/copy;
   already admitted executions may run concurrently. Projects sharing a volume
   should use the same protected store so admission counts all their snapshots.
   A failed copy removes its partial snapshot and never records proof. A crash
   can leave a stale copy; it remains charged against the total until the
   operator confirms it is inactive and removes it. Raise limits only in the
   protected project configuration when the expected artifact requires it.
   Direct executable artifacts need no runner. The worker account's environment
   contains only PATH, HOME and LANG, and it runs from `/`; provide any needed
   test inputs as explicit artifact arguments.
6. Add a reviewed sudoers entry allowing the agent OS group to run ONLY the
   fixed wrapper as root, without arguments. For example:

   ```sudoers
   %ameesh-agents ALL=(root) NOPASSWD: /usr/local/libexec/ameesh-it-proof ""
   ```

   Check the file with `visudo -c`. Do not permit arbitrary Python, ameesh
   commands, configuration paths, environment preservation or wrapper arguments.

An agent declares its delivery with `ameesh module it delivery <lot> --artifact
/path/to/artifact`, then calls `ameesh module it proof <lot> --artifact
/path/to/artifact [--runner /usr/bin/python3] [--timeout 60] -- <artifact args>`.
The CLI passes execution inputs through stdin to the fixed helper. The project
selects a private configuration; the helper rechecks that this project owns the
lot and is active, obtains the current declaration, executes its protected snapshot, checks the snapshot SHA-256
before/after and records evidence under its isolated role. Client-supplied
command/output/exit/digest/binding fields are rejected. Database guards recheck
the declaration under the lot lock before accepting evidence.

The database superuser/migration owner and the root service installation remain
trusted operators. This proves observed execution under the configured runner,
not smoke-test quality or OS attestation. The artifact worker is a credential
boundary, not a complete container sandbox. Platforms without this isolation
cannot produce evidence with this helper; they fail closed rather than submit
caller-declared proof. To undo installation, remove the fixed sudo permission
and wrapper; gates remain active and missing proof continues to refuse closure.
