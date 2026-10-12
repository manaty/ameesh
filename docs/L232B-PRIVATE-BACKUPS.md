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
environment. Upload success alone cannot prove remote recovery. The current server script
expires ordinary local backups only after all required dump/upload steps
succeed.

`retrieve --credentials RECOVERY_CONFIG --host HOST --name OBJECT
--sha256 SHA256 --output ARCHIVE` downloads an exact object using the
separate recovery identity. The owned regular configuration file must be
private (`0600` or stricter) and contains `access_key`, `secret_key`,
`bucket` and `region`. Curl reads a short-lived private configuration;
the child environment excludes inherited upload keys. The request uses a
fixed HTTPS storage origin, with redirects disabled. Only a non-empty
archive matching the pinned SHA-256 and readable by `pg_restore --list`
is published, atomically, without replacing an existing destination.
Failed download, digest or catalogue verification leaves no final artifact
and removes staging credentials. Retrieval proves artifact integrity;
`verify-restore` must still succeed before a release claims remote recovery.
The expected digest and inventory must come from the pinned recovery set,
not from an untrusted remote response.

These utilities are a local development checkpoint. The release controller,
host helper/bootstrap, pinned retention and versioned environment switching
are still being built. Retrieval tests simulate storage and do not claim
an authenticated remote-storage pilot. `tests.test_l232b_backup_restore`
uses the explicit test-bench DSN and newly created source/restoration
databases to exercise real `pg_dump` and `pg_restore`. It verifies that
committed writes after snapshot export do not change the archive inventory,
that a wrong expected count fails verification, and that the disposable
restore database is removed while the source remains intact. It requires
compatible PostgreSQL clients and test-role database-creation permission.
These tests do not claim a completed production backup or release pilot.
