# Private backup preparation (L232b)

The deployment helper `deploy/mise-a-jour/sauvegarder-prive.py` prepares
database archives with private PostgreSQL and storage credential channels.
It runs with the configured backup identity, using the release environment's
Python and psycopg dependency. Credentials are read from private configuration
references; subprocess arguments contain a password-free conninfo and private
passfile/configuration paths.

`dump --config CONFIG --output ARCHIVE` finalizes a non-empty archive only
after `pg_restore --list` succeeds. Directories are `0700`, archives and
short-lived credential files are `0600`. Existing artifacts are retained;
publication refuses replacement. Catalogue readability alone does not establish
restorability.

`snapshot-dump --config CONFIG --output ARCHIVE --inventory INVENTORY
[--schema SCHEMA] [--table TABLE ...]` also captures a private expected
inventory. Archive and inventory must be new files in the same directory.
The source uses a read-only repeatable-read transaction and exports its
snapshot to `pg_dump`; that transaction stays open until the dump finishes.
The archive covers the database, and the inventory records the selected mesh
schema's migration versions/checksums, representative row counts and archive
SHA-256. Defaults are `public`, `agent_registry` and `agent_mailbox`.
Concurrent writes after snapshot export do not change those expected counts.
The inventory is published only after the archive succeeds and contains no
connection credentials. Source configurations that store their mesh in another
schema must name that schema explicitly.

`verify-restore --config RESTORE_CONFIG --file ARCHIVE --expected INVENTORY`
uses the separately approved restore identity. It verifies the archive digest,
creates a new randomly named `ameesh_verify_*` database, restores with
`--exit-on-error`, checks migration checksums and expected row counts, and
removes that database. It never targets an existing database for restoration
or deletion. Failed restore, comparison or cleanup produces no verified proof.
The restore identity therefore needs permission to create its disposable
database; runner credentials are not a substitute for that capability.

`upload --file ARCHIVE --host HOST` uses a private curl configuration, disables
the user's curl configuration and removes upload keys from the child
environment. Upload success alone cannot prove remote recovery: retrieval
using a separate restore identity, remote digest verification and pinned
recovery-set retention remain controller work. The current server script
expires ordinary local backups only after all required dump/upload steps
succeed.

These utilities are a local development checkpoint. The release controller,
host helper/bootstrap, private remote retrieval and retention, and versioned
environment switching are still being built. Snapshot/restore unit tests use
simulated connections; they do not claim a completed production backup or an
end-to-end restored pilot.
