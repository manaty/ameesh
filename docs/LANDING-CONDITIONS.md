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
        "required_statuses": {"integration/**": "lot/"},
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

`ci_required: true` reads GitHub Actions runs, external app checks and classic
commit statuses for the **frozen SHA** before queue admission and again during
landing. It checks before preparing the merge, after the merged-tree gates,
and immediately before a PR merge. Workflows are grouped by their GitHub
database ID, so identical display names cannot hide a failure. Run ordering
uses the latest of creation, start and update timestamps, including reruns of
older runs. Missing identities or invalid timestamps refuse certification.
External checks are grouped by app identity and name and ordered by the latest
start/completion time; unfinished attempts always block. Missing or invalid
activity timestamps and ambiguous latest activity refuse certification.
Every latest workflow and external app check must be completed and successful,
neutral or skipped; classic statuses must be successful. At least one check
must exist. Missing, running, failed, unreadable or potentially truncated
results refuse admission and eject a queued lot before landing.
The branch's current green CI cannot vouch for a different frozen commit.

`required_statuses` maps a branch glob to a commit status context prefix. A
PR head, or a frozen commit, of a matching branch is green only when at least
one classic commit status with that prefix is `success`; the other checks
still apply. Only a commit status counts (a check run's name is free text),
and an unknown branch gets every declared prefix. It applies wherever the PR
rollup or the frozen commit is read: queue admission, landing, and
`ameesh work merge-pr`. A head whose checks are all green but whose required
status is not yet published reads `pending`, never green. Nexlink declares
`{"integration/**": "lot/"}`: a package is validated by its `lot/<suite>`
statuses, so a package bootstrapper that ends with `success` and `skipped`
checks alone (a change set making no part due) cannot land it (#1748).
Ambiguous project configurations for the same repository are refused at
admission and landing, even from an exact configured checkout without origin.
GitHub repository path case is normalized for admission and queue ownership,
including entries queued before normalization was introduced.
Landing also requires the queue entry’s normalized source to match the selected
project’s repository. A different or missing source refuses landing before any
lock or queue mutation; select the correct project or requeue a legacy entry.
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
