# Repository landing conditions

A repository can declare generated files, local gates chosen by changed
paths, CI required on the frozen commit, and protected targets in the host's
`radar.projects` configuration. These conditions supplement the queue's
existing freeze, review, generation and lease checks.

```json
{
  "radar": {
    "projects": {
      "product": {
        "repo": "/srv/product",
        "github": "acme/product",
        "target": "develop",
        "migrations": "supabase/migrations",
        "land": "local",
        "lease_ref": "ameesh-land/",
        "ci_required": true,
        "protected": ["main", "desktop-v*", "product-releases*", "refs/tags/*"],
        "generated": [
          {
            "path": "backend/domains.json",
            "regen": "npm run domains-export",
            "check": "npm run domains-export -- --check"
          }
        ],
        "gates": [
          "backend/scripts/check-rollbacks.sh",
          {
            "run": "npm run domains-export -- --check",
            "paths": ["backend/domains/**", "backend/domains.json"]
          }
        ]
      }
    }
  }
}
```

Use the repository's actual commands and branch prefixes. Commands run in
the radar's private clone and need their dependencies installed there.
Changing this configuration does not install dependencies or run a deployment.

Every declared generated file is regenerated and checked on **every merged
tree**, including when a lot only changes an input. Paths must be distinct
repository-relative files. Generated conflicts are removed from the unmerged
index before rerere runs; cached resolutions never supply their content.
The generator's output is staged before its check and the merge commit.
The queue stack applies the same exclusion, regeneration and check rules before
ranking a trial as mergeable. Its cache includes the generated-file configuration.
A failed regeneration or check ejects the lot and leaves the target untouched.
Ordinary conflicts still use the prepared resolution cache.

String gates always run. Object gates run when at least one `paths` glob
matches a file changed by the merge, including newly generated output. If Git
cannot read the diff, all path gates run. Globs use `fnmatch`: `*` can cross a
slash, so a gate may run more often than its GitHub workflow. List an
unconditional gate when every merge needs a minimum check.

`ci_required: true` reads GitHub Actions runs for the **frozen SHA** before
queue admission and again during local landing, including immediately before
the target push after the merged-tree gates. Workflows are grouped by their
GitHub database ID, so identical display names cannot hide a failure. Missing
identities or invalid timestamps refuse certification. The latest run of every
returned workflow must be completed and successful, neutral or skipped; at
least one run must exist. Missing,
running, failed, unreadable or potentially truncated results refuse admission
and eject a queued lot during local landing.
The branch's current green CI cannot vouch for a different frozen commit.
Ambiguous project configurations for the same repository are refused even from
an exact configured checkout, including one without an origin URL.
Authentication uses the host's existing `gh` setup. The local gates then run
on the actual merged tree before the target push. Trial-ref workflows are a
separate future option; this configuration uses local gates plus frozen CI.

`protected` contains target globs. Queue admission refuses them, and land
ejects previously queued entries if they become protected. Bare branch names,
`origin/` names and `refs/heads/` names are treated consistently; `refs/tags/*`
protects all tag refs. Protected branches and releases remain explicit
orchestrator operations outside queue/land.

## Migration reservations

The radar reports numbered SQL files added by an open lot without a reservation
belonging to **that lot in that repository**. It checks three- or four-digit
names, such as `supabase/migrations/323_billing.sql`. Historical files already
in the common base are excluded. A reservation in another repository does not
count, and a number held by another lot is reported with that holder. The
snapshot, `ameesh conflicts`, and the lot's passive radar message show the
finding. This probe observes; it does not reserve numbers or execute SQL.
An unreadable registry is reported as a radar error.

For new work, reserve through the existing command before writing SQL:

```sh
ameesh work migration 123 --repo /srv/product
```

For the existing Markdown registry, use the one-shot import script from a
checkout of ameesh. It reads active rows from a `Number | Lot | Who | State`
table; the legacy French headers and reservation statuses are also accepted.
Merged, released and cancelled rows are excluded. Unknown numbered statuses
and duplicate active numbers are errors.

```sh
python scripts/import-migration-reservations.py \
  /srv/product/supabase/migrations/RESERVATIONS.md --repo acme/product
```

This prints a dry-run report with `unmapped` entries and writes nothing. Prepare
and review a JSON mapping from each active migration number to its **ameesh lot
id**. Issue numbers and legacy lot labels are not guessed:

```json
{"323": 123, "324": 456}
```

```sh
python scripts/import-migration-reservations.py \
  /srv/product/supabase/migrations/RESERVATIONS.md --repo acme/product \
  --mapping /path/to/reviewed-mapping.json
```

Only `--apply --actor <author>` writes the complete mapping. It verifies that
lots are open and belong to the repository, refuses to replace a different
reservation, locks the lot rows, and applies the entire import in one
transaction. The repository/number unique index rejects a concurrent collision;
the whole import then rolls back. Repeating a successful import is a no-op,
including its journal. Legacy SQL may already exist on a branch: this import
records the old reservation rather than allocating a new number. It does not
change `RESERVATIONS.md` or execute any SQL migration.
