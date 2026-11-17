# Weekly stable channel and single consumer pin

Reference: [#7666 / #5858](https://github.com/manaty/ameesh/issues/5858), steps 4
and 5 of [#5827](https://github.com/manaty/ameesh/issues/5827). Implementation:
`src/ameesh/stable_channel.py`, commands `ameesh release stable …`.

The construction version stays free: the 11:00 and 23:00 cutoffs keep tagging
`vX.Y.Z` twice a day. External meshes do not follow those tags. They follow one
**stable** version per week, published as an immutable tag `stable/<week>`.

## Designation rule

The rule is code without judgement (canon §7). It reads only durable facts.

1. The week `W` is the ISO week (`2026-W47`) in the compiled canon timezone.
   Its designation instant `D(W)` is the first compiled cutoff of its Monday
   (11:00 Europe/Paris with the current canon).
2. A release tag `vX.Y.Z` reachable from the target is **eligible** when all of
   these hold:
   - it was tagged at least **24 h** before `D(W)`, so at least one later cutoff
     and a day of staging on the mesh followed it;
   - its commit is green: the same reading as the cutoff (`commit_is_green`);
     a `[skip ci]` release commit inherits from its green first parent;
   - its staging attestation is complete: the agent VMs proved the commit
     active, and the server and workstation proved it or were explicitly
     reported (`read_attestation`, canon §14);
   - its version is not lower than the previous published stable.
3. The stable of `W` is the eligible tag with the highest version.
4. With no eligible tag, the previous stable is **carried** to `W` unchanged
   (`"carried": true`). With no previous stable either, `W` has no stable.
5. A published week is never moved, re-designated or deleted. Its tag is the
   durable fact and the idempotency key of the tick.

The release tick (`ameesh release tick`, every minute on the elected release
host) applies the rule once `D(W)` is past. It runs only for projects that
declare `release.projects.<project>.stable_channel: true`. A failure is
reported in the tick output and never undoes the cutoff. The tick reads the
remote at most once an hour once the week is published.

`ameesh release stable designate --project ameesh` prints the due week's
designation, including each refused tag and its reason. It publishes nothing.

## Publication

The repository is public, so the **first** stable tag is a public publication.
It needs a signed owner decision whose answered option is `publish` (canon §10).
The bureau then runs:

```sh
ameesh release stable publish --project ameesh --decision <id>
```

The decision is re-read before the gesture. It must be answered, signed and
current, and belong to the project. Its id is recorded in the first manifest.
Until that gesture, the tick reports `awaiting-decision` and publishes nothing.
After the first stable, each weekly tag is published by the tick without a
human gesture. This is ordinary channel operation, not a new publication.

## Channel manifest

The message of the annotated tag `stable/<week>` is the manifest (schema
`ameesh-stable/1`). It is canonical JSON, with sorted keys, two-space
indentation and a final newline:

| Field | Meaning |
| --- | --- |
| `schema`, `channel` | `ameesh-stable/1`, `stable` |
| `project`, `week`, `designated_at` | Project, ISO week, `D(W)` in the canon timezone |
| `version`, `tag` | `1.59.0`, `v1.59.0` |
| `tag_object`, `commit` | The annotated release tag object and its peeled commit (40 hex) |
| `repository`, `release_url` | The publishing repository and the release page (wheel, `SHA256SUMS`) |
| `carried`, `previous` | Carried over from the previous week; the previous stable week or `null` |
| `decision` | The owner decision of the first publication, `null` afterwards |

The `stable/<week>` tag points at the same commit as `tag`.

## The module's single pin point

A module pins ameesh in **one** file at the root of its repository:
`ameesh-stable.json`. The file is the verbatim channel manifest. Every
executable check derives the ameesh identity from it: the image build, CI,
the upgrade rehearsal, the installed-version check and the advertised version.
No other file names an ameesh version, tag or commit. Historical upgrade
evidence may keep old versions, labelled as history.

To move up to the next stable:

```sh
git clone --bare --filter=blob:none https://github.com/manaty/ameesh "$TMPDIR/ameesh.git"
ameesh release stable pin --repo "$TMPDIR/ameesh.git" > ameesh-stable.json
ameesh release stable verify ameesh-stable.json --repo "$TMPDIR/ameesh.git"
git commit -m "ameesh stable $(jq -r .week ameesh-stable.json)" ameesh-stable.json
```

`pin` prints the latest published week (`--week YYYY-Www` selects another).
`verify` refuses a pin that differs by a single byte from the published
manifest. It also refuses a pin whose release tag object or commit on the
remote is not the pinned one. With `--installed`, it also refuses an ameesh
installation of another version. Use that check in the image and at runtime.

Without the ameesh CLI, the same identity is readable with git alone:
`git cat-file tag stable/<week>`, whose message is the pin file. Then check
`git rev-parse v<version>` and `v<version>^{commit}` against `tag_object` and
`commit`.

The montée changes only `ameesh-stable.json`. The module code that uses only
the [public contract](module-contract-v1.md) is unchanged between two stable
versions. The contract's compatibility rule (no break in a minor version,
deprecation one version before) is what makes this hold. The module witness of
[#7665 / #5857](https://github.com/manaty/ameesh/issues/5857) checks it in
ameesh's CI.
