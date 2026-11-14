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
result. A missing file, malformed policy, unreadable SQL or doubtful analysis
returns nonzero with a named reason. A missing policy uses the generic SQL
defaults: additive SQL passes, undeclared relaxation or destruction refuses.

The generic defaults live in ameesh. They inspect static SQL inside understood
DO blocks, retaining the SQL of every branch and taking the worst class.
Function bodies are stored definitions, rather than SQL run by CREATE. Dynamic
EXECUTE, unknown procedural conditions and malformed blocks remain doubtful
and refuse. The real 796/797 incident files are regression fixtures: both
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

For a destructive migration, the entry contains `checksum`, `lot`, `decision`
(the integer decision ID), and `approval_option` (the key of the owner's
agreement option). The command reads the ordinary ameesh decision proof
channel only when it encounters a destructive file. The decision must be
answered on that carrier lot and project, with the specified agreement option.
The existing `verify_proof` and `require_current` checks re-verify the owner
signature and reject superseded authority. The owner answer needs no special
text format. The reviewed policy entry binds the file digest to this decision.

The release handoff's Git delta is only advisory: it can include files already
applied in staging. Its `migration_safety` report and handoff message warn about
non-additive or doubtful files but do not block the release or read the live
staging ledger. Only the explicit applicator inventory is a blocking check.

Project application is separate: Nexlink's applicator integration is #7369;
ameesh's staging integration has its own application lot. A project's gate is
active only once that integration is delivered. Manually executed SQL outside
the applicator is outside this control.
