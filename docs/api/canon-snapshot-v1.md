# Public canon and directory snapshot, version 1

E1a, [lot #7669 / issue #5862](https://github.com/manaty/ameesh/issues/5862),
under [the external-module contract](module-contract-v1.md).
Consumer adoption/removal remains in
[IMA#1803](https://github.com/thepackengers/issue-manager-agent/issues/1803).

## Invocation and authority

```sh
ameesh canon snapshot --canon /path/to/home --ref origin/main --json \
  --expect-commit FULL_TRUSTED_SHA --previous-commit PREVIOUS_FULL_SHA \
  --expect-scope ameesh --single-source
```

`--expect-commit` is required: a complete lowercase Git object ID (40 or 64
hexadecimal characters), obtained by the caller from its trusted source.
Branch names and abbreviations are refused. It binds the selected canon root,
not the installed ameesh package. Select exactly one configured canon ID or
directory with `--canon` when several are configured.

The command always prints one JSON document, with or without `--json`.
Exit 0 means `ok: true`; exit 1 means a machine-readable refusal with no
snapshot content. Invalid CLI syntax retains the normal parser exit 2.
There is no database connection, registry write, canon synchronization,
execution dispatch or HTTP endpoint. This uses the caller's local filesystem
access; it creates no delegated credential or new permission.

Optional checks:

- `--previous-commit`: prove that the root pin descends from the previous
  full root commit, including equality. An unknown object, unrelated history
  or rewind is refused. The caller persists this high-water mark; the command
  never changes one in the registry or silently allows a rewind.
- `--expect-scope DIR`: require the root's declared, normalized ameesh scope
  to be exactly this directory. A missing or broader scope is refused. Select
  the repository root with its federation manifest to retain its scope
  declarations; the normal canon loader interprets that manifest.
- `--single-source`: explicitly request only the root source; refuse any
  additional source whose declared scope is readable. A declared sibling
  bundle that stays unloaded — absent locally, not a Git repository, missing
  the selected ref, outside the workspace, or with an unreadable scope — is
  listed in `omitted_members`, and a diagnostic that concerns only that
  sibling neither refuses the snapshot nor appears in `findings`; it acquires
  no authority from the result. An error of the root still refuses. Without
  this flag an absent federation member refuses the snapshot; every loaded
  source is pinned and checked independently. The root pin alone is not a
  promise that all sources come from one repository.
- `--fetch`: explicitly fetch before loading, using the existing canon loader.
  A failed fetch refuses the result instead of authorizing cached refs.
  Without it, there is no network refresh. Fetch updates local tracking refs;
  all other snapshot processing is read-only.

For every source the snapshot exports, the selected ref and HEAD must match
the loaded immutable commit, and the working tree must be clean (including
ordinary untracked files). These conditions are checked before
validation/projection and again before success. A moving ref or checkout,
dirty tree, unreadable/untrusted source, unavailable Git observation,
repository mismatch, omitted requested federation member (without
`--single-source`) or validation error yields a refusal. Content is read from
commit objects, never from working-file edits.

Trust means **Git content pinned to the caller's expectation and selected
canon refs**, not an owner signature, a live lease, synchronization to a host,
or approval to execute. This interface does not replace `canon sync` in IMA's
admission/apply path.

## Wire document: `ameesh-canon-snapshot/1`

Every response has these fields:

| Field | Type and meaning |
| --- | --- |
| `schema` | Constant `ameesh-canon-snapshot/1`. |
| `ok` | Boolean success/refusal. |
| `snapshot_id` | On success, `sha256:` plus the digest of the canonical UTF-8 `snapshot`; otherwise null. |
| `snapshot` | Immutable content object below; null on every refusal. |
| `findings` | Array of `{code, severity, member, path}` from validation. No raw message or card body. Card locations are relative to the source. |
| `refusals` | Array of `{code, member}`; member is a source identifier or null. Nonempty on refusal, empty on success. |
| `freshness` | Separate observation envelope below; never part of the immutable digest. |

`snapshot` contains:

| Field | Content |
| --- | --- |
| `canon` | Canon identifier. |
| `root_commit` | Exact expected full root commit. |
| `omitted_members` | Sorted IDs of unloaded sibling bundles outside an explicitly requested single-root slice. Empty for a complete federation snapshot; never evidence about those members’ contents or authority. |
| `sources` | Sorted array of `{member, commit, ref, prefix, scope, trust}`. `prefix` is relative to its repository; `scope` is a normalized directory list or null; `trust` is `git-pinned`. No raw remote URL or local checkout path. |
| `members` | Sorted array of `{title, roles, deputies, superiors, ref, directory}`. Roles/deputies/superiors retain declared lists or null; `ref` binds the card to its source commit. |
| `agents` | Sorted array of `{name, type, responsible, team, roles, projects, aliases, capabilities, ref, directory}`. Projects and aliases are arrays; nullable declarations retain null. This is the canon directory, not the live registry. |

`directory` preserves that explicitly named metadata as JSON, or null if
absent. Other unknown frontmatter and all bodies are excluded. Member
authenticators and local-account bindings are not exported. Non-JSON metadata
(including nonfinite numbers) refuses the snapshot instead of coercing
arbitrary objects into the wire format.

Content arrays are sorted by member/prefix/commit or identity/card reference.
The digest is SHA-256 of:

```python
json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
           separators=(",", ":"), allow_nan=False).encode("utf-8")
```

| `freshness` field | Meaning |
| --- | --- |
| `observed_at` | Successful final observation time, Unix epoch seconds; null on refusal. |
| `refs_verified` | True only after the final source checks succeed. |
| `scope` | `local-refs` by default; `fetched-refs` with `--fetch`. On refusal this labels the requested mode, not a successful fetch. |
| `remote_current` | Null: a local ref or completed fetch cannot prove the remote will not subsequently advance. |

Freshness is relative to the trusted pin and selected refs at observation
time. An old pin is refused when it differs from the selected root ref.
Deliberately selecting a historical immutable ref does not make it the latest
remote revision. Clients decide when to acquire a new trusted pin and repeat
the read; an old saved success must not serve as current execution authority.

Common refusals include `expected-commit-mismatch`, `non-monotonic-source`,
`expected-scope-mismatch`, `unexpected-source-count`, `source-moved`,
`source-head-diverged`, `source-worktree-dirty`, `canon-fetch-failed`,
`canon-unreadable`, `canon-untrusted`, `source-state-unavailable` and
`snapshot-data-invalid`. Validation codes can also be refusal codes.
Fail closed on unknown refusal codes and ignore additive response fields.

## Contract evidence

`tests/test_7669_canon_snapshot.py` invokes the public CLI in a separate
process against disposable Git repositories and an unreachable database DSN.
It covers metadata/identity projection without private field leakage, stable
content IDs, stale pins, rewind/unknown ancestors, malformed pins, scope
broadening, dirty/divergent sources, moving refs and edits during projection,
untrusted/missing sources, failed and successful fetch, invalid metadata,
additional-source checks and redacted validation findings. In-flight race
tests use the internal loader only to inject timing.
