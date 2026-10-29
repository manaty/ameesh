# Private backup preparation (L232b)

The deployment helper `deploy/mise-a-jour/sauvegarder-prive.py` prepares
database archives with private PostgreSQL and storage credential channels.
It runs with the configured backup identity, using the release environment's
Python and psycopg dependency. Credentials are read from private configuration
references; subprocess arguments contain a password-free conninfo and private
passfile/configuration paths.

`dump --config CONFIG --output ARCHIVE` finalizes a non-empty archive only
after `pg_restore --list` succeeds. The parent directory is created `0700`, and
a directory this process already owns is tightened to `0700`; a shared
directory (another owner, world-writable or sticky, such as `/tmp`) is refused
as it is, never narrowed under its other users. Archives and short-lived
credential files are `0600`. Existing artifacts are retained;
publication refuses replacement. Catalogue readability alone does not establish
restorability.

`snapshot-dump --config CONFIG --output ARCHIVE --inventory INVENTORY
[--schema SCHEMA] [--table TABLE ...]` also captures a private expected
inventory. Archive and inventory must be new files in the same directory.
The source uses a read-only repeatable-read transaction and exports its
snapshot to `pg_dump`; that transaction stays open until the dump finishes.
It disables the server's `idle_in_transaction_session_timeout` for that
transaction only (`SET LOCAL … = 0`, #3836/#3844): a 180 s idle guard on the
mesh database must not take the deliberate snapshot holder for an abandoned
client when a dump outlives it (the session is read-only, dies with the
process, and `pg_dump` already exempts its own session the same way).
The archive covers the database, and the inventory records the selected mesh
schema's migration versions/checksums, representative row counts, archive
SHA-256 and the uncompressed live size (`source_bytes`, the copy a verified
restore materialises in the target cluster). Defaults are `public`,
`agent_registry` and `agent_mailbox`.
Concurrent writes after snapshot export do not change those expected counts.
The inventory is published only after the archive succeeds and contains no
connection credentials. Source configurations that store their mesh in another
schema must name that schema explicitly.

`source_bytes` is mandatory and bounded (K2): the inventory's key set is exact,
and a document that lacks it — an inventory written before the K2 bound — is
refused by the controller instead of being restored on a declared or guessed
size. There is no operator-supplied size and no fallback: a pair that cannot
prove its uncompressed bound is recreated from the live source
(`snapshot-dump`, a re-dump) before any copy, and the restore-capacity
admission keeps its bound over that exact value. L232b is not deployed, so no
inventory predating the bound exists in production.

`verify-restore --config RESTORE_CONFIG --file ARCHIVE --expected INVENTORY`
uses the separately approved restore identity. It verifies the archive digest,
creates a new randomly named `ameesh_verify_*` database, restores with
`--exit-on-error`, checks migration checksums and expected row counts, and
removes that database. A private marker names that database before it is
created, so a verification killed before its cleanup is reaped by the next
one: no cleartext copy survives without a name. It never targets an existing
database for restoration or deletion; only the exact `ameesh_verify_*` names
carried by its own private markers are dropped. Failed restore, comparison or
cleanup produces no verified proof.
The restore identity therefore needs permission to create its disposable
database; runner credentials are not a substitute for that capability.

`upload --file ARCHIVE --host HOST [--receipt RECEIPT]` uses a private curl
configuration, disables
the user's curl configuration and removes upload keys from the child
environment. The controller companion
(`sauvegarde-release.py`, below) calls the same function with an explicit
credential document (`storage_credentials(path)`, exact private
`access_key`/`secret_key`/`bucket`/`region`) and an explicit private on-disk
staging directory, so an upload never depends on inherited `SCW_*` variables or
a shared tmpfs. Upload success alone cannot prove remote recovery. The server script
runs catalog-based local expiry only after all required dump/upload steps
succeed. Backups without a registered policy remain protected.

Every signed request names `x-amz-content-sha256`: the file's SHA-256 for the
upload PUT, and the empty-body digest for the retrieval GET (as well as the
version-retention GET and DELETE). curl 7.88.1, shipped by Debian 12, signs
that header when it is supplied but never adds it by itself; Scaleway then
refuses the request (HTTP 400 `Required header missing`). The wire contract is
witnessed against a local TLS fake S3 with the real curl 7.88.1: the request is
refused without the header, and succeeds with it, the header appearing in
`SignedHeaders`.

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

`pull --output ARCHIVE --sha256 SHA256 [--bytes N] [--timeout SECONDS]`
receives a gzip-compressed archive on standard input — the guarded local dump
that `ameesh-sauvegarde` already produced on the VM, streamed over SSH — and
decompresses it in a private staging directory. The archive is published
atomically only after its exact attested size and SHA-256 match — the
attestation comes from the same SSH-authenticated host as the stream, so it
proves the integrity of the transport, not the authenticity of the artifact
vis-à-vis the pinned recovery set; a timeout reports the bytes already
written. It runs no database client and receives no DSN: the poste's backup
path no longer sends `pg_dump` through WireGuard.

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
`find -delete` path. A copy published by the daily server backup and named by
no catalog record keeps its declared local window (`expire-unregistered
--max-age`, 14 days for the daily dump) and is removed by age; a set
registered in the catalog (for example by the release controller) is never
touched by that age path, whatever its age or state. A refused candidate — a
daily dump that is publicly readable, shared or owned by another identity — is
never deleted and makes the `expire-unregistered` command exit non-zero; the
daily server script keeps that status, still runs `prune`, then fails, so a
file that will never expire cannot leave the timer silently green. So expiry
stays explicit
and catalogued, while the daily dump cannot fill the database disk without
bound. Unregistered artifacts that predate this rule are treated the same way
and expire on the first completed run after the window. Disk admission must
still count catalogued artifacts and hold a release when capacity is
insufficient. No remote object deletion is introduced: the daily upload path
keeps no version receipt, so its remote copies follow the operator's own
bucket policy.
An optional private `--remote-inventory FILE` registers exact `object_id`
(`HOST/NAME`) and `sha256` entries for remote copies. Each digest must match
a member of the local set, and object references cannot belong to two sets.
The controller supplies this inventory only after verified remote retrieval
and restoration. Registration still does not perform that verification.

For these sets, the controller uses the Python `prune(..., remote_adapter=...)`
interface. The adapter must inspect pinned object digests and perform an
**atomic conditional** `delete_if_digest`, returning an exact object/digest
absence receipt. An inspect followed by unconditional deletion cannot satisfy
this contract. The entire eligible batch is checked before deletion; a pinned
remote digest that changed or disappeared in that preflight refuses the whole
batch, and the error names the artifact and object to reconcile — a set that
can no longer be proven complete is settled before any other candidate is
expired, and nothing is deleted meanwhile. Remote
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
environments are still being built. The durable ordering engine is described in
[L232b controller protocol](L232B-RELEASE-CONTROLLER.md); its authority and host
adapters are simulated and not enrolled for execution. Retrieval tests simulate storage and do not claim
an authenticated remote-storage pilot. `tests.test_l232b_backup_restore`
uses the explicit test-bench DSN and newly created source/restoration
databases to exercise real `pg_dump` and `pg_restore`. It verifies that
committed writes after snapshot export do not change the archive inventory,
that a wrong expected count fails verification, and that the disposable
restore database is removed while the source remains intact. It requires
compatible PostgreSQL clients and test-role database-creation permission.
These tests do not claim a completed production backup or release pilot.

The current transfer endpoint is Scaleway Object Storage. Its
[object operations documentation](https://www.scaleway.com/en/docs/object-storage/api-cli/object-operations/)
describes deletion of an explicit `versionId`; an unqualified delete in a
versioned bucket creates a delete marker. Its
[conditional writes documentation](https://www.scaleway.com/en/docs/object-storage/api-cli/using-conditional-writes/)
describes conditional uploads, not a conditional DeleteObject contract.
Inference for the unfinished adapter: support for conditional uploads is
insufficient evidence for safely deleting a digest-pinned recovery object.
Either verify conditional-delete semantics for the enrolled backend or pin an
immutable provider version through upload, retrieval, verification and expiry.
Legacy name/digest inventories do not capture those version ids. The optional
version-specific path below adds them; no bucket-versioning or storage-policy
change has been made by this lot.

## Version-specific storage preparation

`upload --receipt FILE` optionally publishes a private no-replacement JSON
receipt with the object key, SHA-256, bucket/region and non-null provider version
reported by the completed upload. Missing/duplicate/null version headers or a
changed archive produce no receipt. Upload completion still does not establish
restoration. Failed or retried uploads may leave unregistered provider versions;
these utilities do not delete them implicitly.

`retrieve --version-id ID` addresses that exact provider version, validates the
response version as well as the pinned SHA-256 and archive catalogue, and then
publishes the private local artifact. A same-digest reply naming a different
version is rejected. Use the isolated restore verification before registration.
Curl children now receive a minimal environment; inherited upload credentials,
proxy and certificate overrides cannot select another transfer identity/origin.

A remote inventory item may include `version_id`. Registration preserves it;
null versions, duplicate exact version references and mixing an unqualified
reference with qualified versions at the same key are rejected. Distinct exact
versions at one key can have separate holds and policy deadlines. Pruning passes
the version to both adapter methods and requires it in the exact absence receipt.
Legacy name/digest references still require an adapter capable of atomic
conditional deletion; they cannot use the version-specific backend.

`retention-scaleway.py.VersionRetention(credentials, staging_root, max_bytes=N)`
implements version-specific GET/hash, DELETE and GET/absence confirmation against
the fixed Scaleway HTTPS origin. Each request includes an encoded non-null
`versionId`; returned retrieval headers must identify that exact version.
The digest check remains safe between GET and DELETE by addressing an immutable
version rather than the mutable latest key. It never creates a delete marker by
issuing an unqualified deletion and never addresses a concurrent newer version.

The separate private credential document has `access_key`, `secret_key`,
`bucket`, `region`, `purpose: "retention-versions"` and `allowed_hosts`.
Enrollment must bind that exact bucket/region and host allowlist to the approved
retention policy and supply a dedicated identity scoped to version retrieval and
deletion. The purpose marker checks configuration shape, not actual provider IAM
permissions. Neither an upload nor recovery key is automatically reused.
The backend performs no enrollment, bucket-versioning change or privilege grant.

Credentials use short-lived `0600` curl configuration files under private owned
on-disk staging. Explicit capacity and object-size budgets, bounded deadlines
and curl 7.75+ (the `--aws-sigv4` floor; Debian 12 ships 7.88) are required. The
[maximum-size option](https://curl.se/docs/manpage.html#--max-filesize)
stops a transfer whose length is known in advance; for a response of unknown
length the curl child carries its own kernel `RLIMIT_FSIZE`, so no response can
be written past a bounded size whatever the provider sends (curl only enforces
`--max-filesize` on unknown lengths from 8.4, which the enrolled hosts do not
ship). The controller must
reserve concurrent staging capacity; the backend's free-space check is not that
reservation. Errors contain categories, never raw provider diagnostics.
Only a provider object/version-not-found response proves absence; missing bucket,
redirect, access denied, corrupt headers, wrong hash, timeout and an unconfirmed
DELETE preserve the local recovery set. Lost deletion replies resume under the
catalog's already durable intent. Temporary response/configuration files are
removed on success and failure.

This backend is source code only: no provider was contacted and no provider
policy was changed. `ameesh-retention-privee.py prune --provider-enrollment`
resolves it only when a candidate still has an undeleted remote member: the
protected document has exact keys `host` (one enrolled object host),
`credentials` (private provider credential file), `staging` (private on-disk
staging root) and `max_bytes` (explicit staging budget). The backend module is
the fixed sibling installed by `installer-serveur.sh`; a caller cannot name
another module or path. The enrolled host must already be covered by the
credential's `allowed_hosts`, and a scoped adapter refuses any object whose
host differs from the enrollment. A missing, linked, open, oversized or inexact
document, an unenrolled or untrusted module, or a version-less remote reference
keeps the local set protected. `sauvegarder-base.sh` passes
`/etc/ameesh/retention-provider.json` when it exists, and otherwise keeps
remote sets protected as before. Tests simulate the HTTP transport, including
credentials and argv canaries, concurrent new versions, lost replies,
quota/size failures and version holds. No authenticated Scaleway request or
provider-policy pilot has been performed. `sauvegarde-release.py` binds
`backup`, `pin` and `accept` to these primitives (see
`L232B-RELEASE-CONTROLLER.md`); host enrollment of the document and the
authenticated pilot remain unfinished; the presence of this module is not
deployment authority.
