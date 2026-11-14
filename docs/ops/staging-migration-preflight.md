# Staging migration preflight

Before a Nexlink staging handoff publishes its release tag, the release
controller reads `.ameesh/staging-migrations.json` from the frozen candidate.
If the candidate has migrations, an absent or unreadable policy refuses the
handoff. No SQL is executed by this check.

Start with `deploy/staging-migrations-policy.example.json`, then review the
policy in the project repository. Its `rules` contain regular expressions and
reasons for `additive`, `relaxation`, and `destructive` statements. An entire
statement must match an allowed shape. Destructive matches take priority over
relaxation and additive matches. Unrecognised statements, malformed SQL and
anonymous procedural blocks refuse with a named reason. Function bodies are
not executed by this preflight or inferred to be safe. The example is
deliberately restrictive; a legitimate but unrecognised migration needs policy
review, rather than an automatic exemption.

For a relaxation, add a declaration keyed by the full migration path:

```json
{
  "checksum": "<SHA256 of the SQL bytes>",
  "lot": 1234,
  "restore": "supabase/rollback/796_restore.sql"
}
```

The carrier lot must belong to the release project and contain this exact note
(replace the path and digest):

```text
staging-migration:supabase/migrations/796_example.sql:<SHA256>
```

The restore path must be relative, end in `_restore.sql`, and exist with
nonempty contents in the frozen candidate. This proves its presence, not that
it restores the schema correctly; review and the migration's rollback tests
must establish that.

For a destructive migration, the declaration instead contains `checksum` and
`decision` (the integer decision ID). Its answered owner decision must contain
the exact authorization line:

```text
project:owner/repository allow-staging-migration:supabase/migrations/796_example.sql:<SHA256>
```

The controller re-verifies the owner signature and decision currency through
the normal decision verifier. A signed decision for another repository or SQL
digest does not authorize this migration. Unknown SQL cannot be authorized by
this declaration mechanism.

The returned handoff manifest records the classification and reasons under
`migration_safety`. A preflight saved by an older controller without that field
must run the check before publishing. This gate covers the controller's
staging handoff; it cannot prevent SQL applied manually outside that flow.
