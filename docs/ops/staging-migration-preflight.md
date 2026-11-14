# Check the pending staging migrations

The blocking check receives the applicator's actual pending inventory. It
reads the specified files and optional `.ameesh/staging-migrations.json` from
one exact Git commit. It never applies SQL, modifies the checkout, or reads a
staging credential.

```sh
ameesh staging-migrations --repo /path/to/project --sha FULL_COMMIT_SHA \
  --project project-name --file path/to/001_example.sql --file path/to/002_example.sql
```

For a longer or empty inventory, pass `--files-from FILE`, containing one
repository-relative path per line. This local inventory file only selects
paths: SQL and policy contents always come from the frozen Git tree. The
applicator must check precisely the files and commit it will apply, and stop
when the command returns nonzero. Output is a JSON report with a per-file
result. A missing file or malformed policy returns nonzero with a named
reason. Doubtful or unreadable SQL requires a signed owner agreement, as for
destructive SQL. A missing policy uses the generic SQL
defaults: additive SQL passes, undeclared relaxation or destruction refuses.

The generic defaults live in ameesh. They inspect static SQL inside understood
DO blocks, retaining the SQL of every branch and taking the worst class.
Function bodies are stored definitions, rather than SQL run by CREATE. Dynamic
EXECUTE, unknown procedural conditions and malformed blocks remain doubtful
and refuse without that signed agreement. A restore declaration alone cannot
authorize doubtful SQL. The real 796/797 incident files are regression fixtures: both
classify as relaxation and require a declaration and restore file.

The project policy schema is `ameesh-staging-migrations/1`. Start with
`deploy/staging-migrations-policy.example.json`. Its `declarations` object is
keyed by the full migration path. Optional `exceptions` contains reviewed
project SQL forms, grouped by `additive`, `relaxation` or `destructive`; each
entry has `pattern` (whole-statement regular expression) and `reason`.
Exceptions cannot override a matching destructive default.

For a relaxation, add this entry before applying:

```json
{
  "checksum": "<SHA256 of the SQL bytes>",
  "lot": 1234,
  "restore": "rollback/001_restore.sql"
}
```

The frozen entry itself is the declaration reviewed with the migration's lot.
No extra note is required. The restore path must be a regular nonempty file in
the same frozen tree, with a name ending in `_restore.sql`. This checks its
presence; migration review and rollback tests establish its correctness.

For a destructive or doubtful migration, the entry contains `checksum`, `lot`, `decision`
(the integer decision ID), and `approval_option` (the key of the owner's
agreement option). The command reads the ordinary ameesh decision proof
channel only when it encounters a destructive or doubtful file. The decision must be
answered on that carrier lot and project, with the specified agreement option.
The existing `verify_proof` and `require_current` checks re-verify the owner
signature and reject superseded authority. The owner answer needs no special
text format. The reviewed policy entry binds the file digest to this decision.

The release handoff's Git delta is only advisory: it can include files already
applied in staging. Its `migration_safety` report and handoff message warn about
non-additive or doubtful files but do not block the release or read the live
staging ledger. Only the explicit applicator inventory is a blocking check.

Project application is separate: Nexlink's applicator integration is #7374.
ameesh's staging sequence is integrated (#7368): before its migrations step,
and again in the read-only preflight, it computes the migrations actually
pending on the target database with `ameesh.staging_migrations_preflight`
(read-only `schema_migrations` query, migrations directory of the frozen
snapshot), then runs the gate on exactly those repository paths and stops
the sequence on a refusal, before any SQL write and any executor restart. The
repository now carries an empty `.ameesh/staging-migrations.json`; a project
declaration is added to that file with the migration's lot. A project's gate is
active only once its applicator integration is delivered. Manually executed
SQL outside the applicator is outside this control.

A new `ADD COLUMN ... NOT NULL` is additive only for a recognized built-in
column type (optionally qualified by `pg_catalog`) with a constant default:
a quoted literal, boolean, number or empty array (optionally with a built-in type cast from the recognized list).
Absent defaults, NULL, function calls and computed expressions retain the
destructive classification. Custom types and domains also remain destructive:
a domain CHECK can reject the default on an N-1 insert even if adding the
column to an empty table succeeds. This lets N-1 writers omit the new column: 0576
(`profiles text[] not null default '{}'`) is additive.

A new trigger is a relaxation requiring a declaration and frozen restore:
it can reject writes from N-1 code. Replacing and dropping triggers retain
their existing classes. Migration 0572 therefore requires a declaration and
restore. Migration 0575 also requires them because `CREATE OR REPLACE FUNCTION`
is a relaxation, even alongside new tables. The complete 799 fixture remains
doubtful because its outer DO contains an unsupported RAISE EXCEPTION;
classifying its new trigger as relaxation does not authorize other statements.
