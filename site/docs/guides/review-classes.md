# Review classes

Not every change deserves the same review. A one-line style fix and a database
migration should not wait in the same queue. ameesh lets the canon declare,
**by file scope**, which review a change requires, and computes the class of a
change from the files it touches.

## The three classes

| Class | Meaning |
|---|---|
| `light` | merge as soon as the targeted tests are green; review afterwards |
| `normal` | one review by a reviewer from another vendor than the author's; details do not block |
| `sensitive` | freeze, review before merge, explicit agreement: SQL, security, native code, contracts, production |

**When in doubt, the higher class.** The class of a file is the highest of the
rules that match it; the class of a change is the highest of its files. A file
no rule names gets `default` (`normal` if absent).

## Declaring it in the canon

In `federation.yaml`, under `review_policies` (the OKF Federation key):

```yaml
review_policies:
  self_approval: forbidden        # OKF Federation: kept, not interpreted by ameesh
  risk_classes:
    default: normal
    rules:
      - paths: ["**/*.sql", "migrations/**"]
        class: sensitive
      - paths: ["docs/**"]
        class: light
```

Patterns are repository path globs: `*` does not cross `/`, `**` does, and
`docs` does not cover `docs/a.md` (write `docs/**`). `ameesh canon check`
validates the declaration; a policy without `default` gives a warning, not an
error.

## Computing the class of a change

```bash
ameesh review-class docs/intro.md src/app.py migrations/0001.sql
ameesh review-class --diff origin/main            # files changed since merge-base(REF, HEAD),
                                                  # working tree included, renames and untracked files
ameesh review-class --json --diff origin/main     # machine-readable
```

Example output for the first command, with the policy above (labels are
printed in French):

```
classe : sensible
  docs/intro.md                                        léger     docs/**
  migrations/0001.sql                                  sensible  **/*.sql, migrations/**
  src/app.py                                           normal    défaut
```

The `--json` form gives `class`, `default_used`, and for each file its `path`,
`class` and matching `rule`, plus the canon commit the policy was read from
(`policy_ref`). Like every canon read, the policy comes from the merged
canonical branch (`--canon`, `--ref`, `--fetch` work as for `ameesh canon`).

## Delay metrics per lot

The same lot lifecycle records milestones (requested → frozen → verdict →
merged), declared with `ameesh work milestone`, so that the time spent in each
phase and the number of blocked verdicts can be read in `ameesh work list`
(column DÉLAI), `ameesh work show` and `ameesh progress`. See
[Progress view](progress-view.md#lot-milestones).
