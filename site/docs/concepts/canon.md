# The canon

The **canon** is the source of truth for *declarations*: teams and their
responsibilities, hosts and their policies, agents, placements, review
policies and the passkeys each human has enrolled. It is a set of git
repositories in the **OKF v0.2** format, federated according to **OKF
Federation**. Any other tool (a project page, an issue tracker) is a *view* of
the canon; a change made there becomes a **proposal**, never a second source.

ameesh reads only the YAML **frontmatter** of OKF files. Unknown keys are kept
and ignored, so a canon can carry more than ameesh needs.

## Where ameesh reads it

`AMEESH_CANON` (or `canon` in the configuration file) points to the root of an
OKF bundle. If a `federation.yaml` is present there, ameesh also follows the
federation members that are present locally (`workspace_path`).

### Only the approved source

ameesh does **not** read the working files of the clone. It reads the canon at
the **merged revision of the canonical branch**, through git objects
(`git show <commit>:<path>`), and records that commit with every value it
synchronises. The canonical revision is, in order:

1. `AMEESH_CANON_REF` (config `canon_ref`), for example `origin/main`;
2. else the root member's `ref` in `federation.yaml`, as `origin/<ref>`;
3. else `origin/main`.

Uncommitted edits and unpushed local commits are **reported, never used**. A
directory that is not a git repository is read only with
`AMEESH_CANON_UNTRUSTED=1` (tests, prototypes), and every finding is then
tagged as not approved.

### Fail closed

Every `ameesh canon sync` records the canon's status for the host:

| Status | Meaning |
|---|---|
| `ok` | read and valid |
| `invalid` | read, but an error blocks the whole host (federation, unreadable profile card, `Host` card) |
| `unreadable` | root missing, broken repository, revision not found, non-git directory refused |

Agents governed by the canon are **claimable only while their host's state is
`ok`**; no recorded state means not claimable. Leases, sessions and turns
already running are never touched. Agents registered by hand (without a canon
reference) are unaffected.

## Commands

```bash
ameesh canon check [--host H] [--json] [--canon DIR] [--ref REV] [--fetch]   # exit 1 on any error
ameesh canon show  [--json] …                                                # cards read, and from which commit
ameesh canon sync  [--host H] [--json] [--bootstrap-ref BRANCH] …            # canon → registry of host H
```

`canon check` validates the cards (see
[Members, agents, hosts, placements](members-agents-hosts.md#validation)) and
prints the host's placements. `canon sync` writes only **declarative** columns
of the registry (`harness`, `host`, `cwd`, `model`, `budget_usd`,
`responsible`, `team`, `provider`, `credential_mode`, `capabilities`,
`canon_ref`), never state columns (lease, session, status, spend, prompt). An
agent removed from the canon is set `stopped` once its current turn is over;
nothing is killed.

When a canon is configured, the runner runs `canon sync` at start-up and then
every `AMEESH_CANON_SYNC_INTERVAL` seconds (default 300). A failure never stops
the runner; it closes claims through the canon state.

## The trusted branch for passkeys

`canon sync` also copies `Member.authenticators` into the database's trust
registry. The branch that is trusted for this never comes from the commit
being read (its own `federation.yaml` could declare itself canonical). It comes
from the host configuration (`AMEESH_CANON_REF` / `canon_ref`), else from the
last commit already applied, else, on the very first sync, from
`--bootstrap-ref <branch>` (logged, ignored afterwards). The commit read must
be reachable from the trusted remote-tracking branch and descend from the last
applied commit: a sync never goes backwards and never re-activates a removed
passkey.

## Next

- [Members, agents, hosts, placements](members-agents-hosts.md): the card types.
- [Write a canon](../guides/write-a-canon.md): a worked example.
