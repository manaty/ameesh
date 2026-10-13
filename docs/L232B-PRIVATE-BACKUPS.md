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
environment. Upload success alone cannot prove remote recovery. The server script
runs catalog-based local expiry only after all required dump/upload steps
succeed. Backups without a registered policy remain protected.

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

`retention-privee.py --root DIRECTORY register ARTIFACT FILE... --expires-at
EPOCH --rollback-until EPOCH` registers a local recovery set with hashes and
policy dates supplied by the enrolled controller. Include its dump, inventory
and verification artifacts as one set. The private catalog and lock serialize
registration, protection changes and expiry. Registration pins the set until
`accept ARTIFACT` records successful release acceptance, and expiry also waits
until both the policy expiry and rollback deadline have passed. `hold ARTIFACT`
protects it indefinitely; `hold ARTIFACT --release` removes that explicit hold.
The controller must register and pin the verified pre-release set **before**
starting migrations. Registration itself does not prove restoration or grant
controller authority.

`prune --now EPOCH` preflights all eligible files before deleting any. Unknown
files survive; changed, shared, publicly readable or symlinked candidates
block expiry. Artifact ids and durable deletion intent/completion timestamps
remain in `.retention.json`; a retry completes a partially deleted set without
requiring its already deleted members. A hold cannot be added after deletion
has started. Hashing streams large archives in bounded chunks. The authorized
backup/controller identities must use the same lock for protection changes;
this directory is not a security boundary against another process using the
same Unix identity. Direct catalog edits are not supported.

The server installer installs this helper and removes the former age-only
`find -delete` path. Existing and daily backups are preserved until an enrolled
controller registers their policy. This can increase retained disk use; disk
admission must count these artifacts and hold a release when capacity is
insufficient. No implicit 14-day policy or remote object deletion is introduced.
An optional private `--remote-inventory FILE` registers exact `object_id`
(`HOST/NAME`) and `sha256` entries for remote copies. Each digest must match
a member of the local set, and object references cannot belong to two sets.
The controller supplies this inventory only after verified remote retrieval
and restoration. Registration still does not perform that verification.

For these sets, the controller uses the Python `prune(..., remote_adapter=...)`
interface. The adapter must inspect pinned object digests and perform an
**atomic conditional** `delete_if_digest`, returning an exact object/digest
absence receipt. An inspect followed by unconditional deletion cannot satisfy
this contract. The entire eligible batch is checked before deletion; remote
copies are removed and their receipts persisted before local files are
removed. A storage failure or wrong receipt retains the local recovery copy.
A crash after remote deletion but before saving its receipt can replay an
already-absent object only when the catalog has a durable deletion intent.
Confirmed members are skipped on retry. Protection changes use the same lock
and cannot interrupt a deletion already in progress.

No real provider adapter is installed yet. CLI/server pruning therefore
fails closed for eligible sets with unconfirmed remote members, preserving
local files. The simulated adapter tests cover interruption, digest change
between inspect and delete, lost receipt and absent objects. A real backend
must prove conditional deletion for its actual storage provider and use a
separate narrowly scoped retention identity; neither the upload-only key nor
the restore key is implicitly granted deletion authority. Provider adapter
and controller enrollment remain unfinished.

These utilities are a local development checkpoint. The release controller,
host helper/bootstrap, remote retention and installer wiring for versioned
environments are still being built. Retrieval tests simulate storage and do not claim
an authenticated remote-storage pilot. `tests.test_l232b_backup_restore`
uses the explicit test-bench DSN and newly created source/restoration
databases to exercise real `pg_dump` and `pg_restore`. It verifies that
committed writes after snapshot export do not change the archive inventory,
that a wrong expected count fails verification, and that the disposable
restore database is removed while the source remains intact. It requires
compatible PostgreSQL clients and test-role database-creation permission.
These tests do not claim a completed production backup or release pilot.
