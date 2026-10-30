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
        critical: true
      - paths: ["docs/**"]
        class: light
```

Patterns are repository path globs: `*` does not cross `/`, `**` does, and
`docs` does not cover `docs/a.md` (write `docs/**`). `ameesh canon check`
validates the declaration; a policy without `default` gives a warning, not an
error.

Set `critical: true` on a `sensitive` rule for SQL, security or production
paths that require two independent reviews from different model families,
both different from the author's. This declaration applies in every project
policy, including policies under `risk_classes.projects`. It follows the
changed file: another critical path in an unmatched rule does not add a review.
If several matching rules or canons apply, any `critical: true` declaration
wins. A non-boolean value, or `critical: true` on another class, is an error.

Declare new critical paths here when adding them. The review gate uses this
declaration even when its fallback path patterns do not yet recognize a new
module. Those fallback patterns remain a minimum: a recognized SQL, security
or production path still requires two reviews if the canon omits it or calls
it `light` or `normal`, except Markdown documentation (`*.md`) classed `light`
or `normal`, such as `docs/design/decisions/`: only `critical: true` makes it
critical. Ordinary light changes still require no review before merge; other
sensitive paths require one independent review.

## Computing the class of a change

```bash
ameesh review-class docs/intro.md src/app.py migrations/0001.sql
ameesh review-class --diff origin/main            # files changed since merge-base(REF, HEAD),
                                                  # working tree included, renames and untracked files
ameesh review-class --json --diff origin/main     # machine-readable
```

Example output for the first command, with the policy above (labels are
printed in French: `classe` class, `léger` light, `sensible` sensitive,
`défaut` default rule):

```
classe : sensible
  docs/intro.md                                        léger     docs/**
  migrations/0001.sql                                  sensible  **/*.sql, migrations/**
  src/app.py                                           normal    défaut
```

The `--json` form gives `class`, `default_used`, and for each file its `path`,
`class` and matching `rule` (plus `critical: true` when declared), and the canon
commit the policy was read from
(`policy_ref`). Like every canon read, the policy comes from the merged
canonical branch (`--canon`, `--ref`, `--fetch` work as for `ameesh canon`).

## Delay metrics per lot

The development workflow records milestones (requested → frozen → verdict →
merged), declared with `ameesh work milestone`, so that the time spent in each
phase and the number of blocked verdicts can be read in `ameesh work list`
(column DÉLAI), `ameesh work show` and `ameesh progress`. See
[Progress view](progress-view.md#lot-milestones).

See [Work lifecycle](../concepts/work-lifecycle.md) for the distinction between
core states and development milestones.
