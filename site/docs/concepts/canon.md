# The canon

The **canon** is the source of truth for *declarations*: teams and their
responsibilities, hosts and their policies, agents, placements, review
policies and the passkeys each human has enrolled. It is a set of git
repositories in the **OKF v0.2** format, federated according to **OKF
Federation**. Any other tool (a project page, an issue tracker) is a *view* of
the canon; a change made there becomes a **proposal**, never a second source.

ameesh reads only the YAML **frontmatter** of OKF files. Unknown keys are kept
and ignored, so a canon can carry more than ameesh needs.

**Process cards** (`processus/*.md`, `type: Process`) are the working rules
injected into every agent's brief. A card may also carry an executable
`workflow:` block: data compiled to the native `process-workflow/1` schema and
admitted by an exact revision pin. The prose and the workflow live in the same
card; see [Write a Process card](../guides/write-a-process.md).

**Pipeline cards** (`pipelines/*.md`, `type: Pipeline`) declare a pipeline
without code: a `pipeline:` block of the same canon form as a Process card's
`workflow:` block (`workflow:` is still read as a transitional alias; both at
once are refused). `title`, `responsible` (a human `Member`) and `updated` are
required; `name` is optional (default: the file name) and follows the module
name grammar. `ameesh canon check` compiles the block and reports
`pipeline-*` findings: an error fails the check but blocks no agent;
`ameesh canon show --json` lists the cards under `pipelines`. The path rule is
the one of `processus/`: `pipelines/` at the root of the bundle, or under a
folder of the declared ameesh scope. A valid card is also a module of source
`canon` (selected by its `name` in
`extensions.ameesh.projects.<project>.modules`), and
`ameesh module <name> adopt|advance|hold|release` drives a lot pinned on it.
The repository ships the acme and after-sales support examples under
`examples/pipelines/`; see [Write a Pipeline](../guides/write-a-pipeline.md).

## Where ameesh reads it

`AMEESH_CANON` (or `canon` in the configuration file) points to the root of an
OKF bundle. If a `federation.yaml` is present there, ameesh also follows the
federation members that are present locally (`workspace_path`). A host can
also read [several canons](#several-canons-on-one-host) (v1.4.0).

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
of the registry (`harness`, `host`, `cwd`, admitted hosts and tags,
`priority`, `model`, `budget_usd`, `responsible`, `team`, `provider`,
`credential_mode`, `capabilities`, `canon_ref`), never state columns (lease, session, status, spend, prompt). An
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

## Several canons on one host

Since v1.4.0 (design decision 0031), a host can read several canons at once,
for example its own organisation's canon and the canon of a partner, `acme`.
The host's configuration carries a **list** of canons; the **first** is the
**default canon**:

```json
{
  "canons": [
    {"path": "~/canon", "ref": "origin/main"},
    {"path": "~/development/acme/home", "ref": "origin/main"}
  ]
}
```

or `AMEESH_CANONS=~/canon:~/development/acme/home`. A single `canon` keeps
working: it is a list of one. See
[Configuration](../reference/configuration.md#several-canons).

- **Identity.** A canon is identified by the `id` of its `federation.yaml`
  (else by its folder name, with a `canon-id-missing` finding). Two configured
  canons with the same id are a configuration error.
- **References.** Cards of the default canon keep the historical
  `member:path@sha` format; cards of another canon are prefixed with its id:
  `<id>/<member>:path@sha` (for example `acme/home:ameesh/agents/docs-writer.md@…`).
- **Bounded sync.** Each `canon sync` (and the runner, for each canon at every
  pass) reads, stops, removes and retires only the rows of **its** canon.
  Syncing one canon never stops the other canon's agents, and an error in one
  canon does not prevent syncing the others.
- **State per canon.** The canon state is kept per (host, canon): an invalid
  or unreadable canon closes claims of **its** agents only (and of their
  ephemeral agents, which carry their creator's canon).
- **Names are global, first declarer wins.** An agent (or a work package)
  already declared by another canon stays with it: the second canon gets a
  `canon-name-conflict` finding limited to that card, and writes nothing. To
  **move an agent to another canon**, remove its card from the old canon (the
  agent is stopped and its name freed), then declare it in the new one: the row
  changes canon and restarts, with its mailbox and history.
- **Humans are resolved in the agent's canon.** `human:<id>` resolves only
  among the `Member` cards of the agent's canon (or of the host's or work
  package's). A human who works in both canons has a `Member` card in each.
- **Each canon describes the host it uses.** An agent's admission is judged
  with the `Host` card of **its** canon (harnesses, providers, models,
  credential modes, working directories, `max_agents` counted on that canon's
  admissions). A canon without a `Host` card for the host runs nothing there.
  The **physical** limits the runner applies (`policy.resources`,
  `max_agents`) are the **strictest** across the host's `Host` cards in the
  loaded canons; `ameesh hosts` shows where each limit comes from.
- **Authenticators per canon.** Each canon syncs the passkeys of its own
  `Member` cards, with its own journal, monotonicity and trusted branch (the
  `ref` of its `canons` entry; `canon_ref` / `AMEESH_CANON_REF` only for the
  default canon). The same passkey used in two canons has two entries, each
  updated and revoked by its own canon only. A receipt is verified only
  against the authenticators of the canon of the approved action.
- **Tools.** `ameesh canon check|show|sync` and `ameesh placement check`
  process every canon (one block each; `{"canons": [...]}` in JSON);
  `--canon <id>` selects one. `ameesh agent spawn` refuses a name declared by
  any canon.
- **Default names (L243, #468).** `extensions.ameesh.naming` in
  `federation.yaml` (`org`, `projects`, `roles`) yields the new-agent name
  `<org>-<repo>-<role>-<seq>`, or `<org>-<role>-<seq>` for a multi-project
  agent. `ameesh agent name --role R --project P …` prints the next free one
  (cards of every canon and every registry row, stopped agents included, are
  reserved); `ameesh agent spawn` without a name uses the same generator.
  Two repos never share a segment (declared or truncated) and one repo keeps
  the same segment in every canon: a mesh that disagrees is refused. A
  configured canon that cannot be read (revision never fetched, broken repo)
  also refuses generation — its reservations are unknown, and the error names
  the canon and its ref. `canon check` warns (`agent-name-convention`) on a
  card whose title starts with the org prefix but does not follow the
  convention, only when the canon declares `naming:` (existing names are left
  to #423).

Constraints: every host that shares a database must have the **same default
canon**; do not reorder the canons and upgrade in the same step; removing a
canon from the list leaves its rows untouched (its agents are no longer
claimable, nothing is stopped).

### Bounding ameesh to a folder of a shared canon

A canon shared with other tools can contain files whose `type` matches the
ameesh profile without being ameesh cards (for example `type: Agent` files
that describe another tool's sub-agents). Read as ameesh cards, they would make
the whole canon invalid. `federation.yaml` can bound the ameesh cards
(`Agent`, `Host`, `Placement`, `Member`, `WorkPackage`) to one or more folders
relative to the bundle, under `extensions`, the only open key of the OKF
Federation schema:

```yaml title="federation.yaml"
id: acme
root: home
extensions:
  ameesh:
    scope: ameesh          # or a list: [ameesh, teams/ops/ameesh]
    members:               # optional: other federation members
      tools:
        scope: ameesh
members:
  - id: home
    ...
```

- With the key, only cards **under** the scope are read; files of the same
  `type` elsewhere are ignored, with one aggregated `ameesh-scope-ignored`
  information per member. `canon show` prints the scope.
- **Without the key, nothing changes**: the whole canon is read.
- `extensions.ameesh.scope` applies to the root bundle; another member is
  bounded by `extensions.ameesh.members.<id>.scope`. The root manifest decides:
  a member does not bound itself.
- The older form (`ameesh:` at the top level, `members[].ameesh`) is still
  read, with the warning `ameesh-scope-legacy` (it fails the OKF Federation
  validator); `extensions.ameesh` wins when both are present.
- **Fail closed**: an unreadable scope (absolute, `..`, pattern, hidden folder,
  empty list: `ameesh-scope-invalid`) or one missing from the revision read
  (`ameesh-scope-missing`) is an error: no card of that member is read, the
  canon is invalid for its agents, and nothing is stopped or retired.
- Declaring a scope on a canon already synced **retires** the cards left
  outside it: move the cards under the scope in the **same** pull request as
  the key.

### Module activation

The same `extensions.ameesh` mapping also enables installed modules at three
scopes: `modules` (the whole canon), `teams.<team>` (the projects its packages
and agents carry as `team`) and `projects.<project>`. The narrowest scope that
names a module gives its options; a project always wins. A malformed or unknown
activation fails `canon check`; a refused entry point that no scope activates
is only a warning. `ameesh modules --active` lists the applied project modules
and `ameesh doctor` checks them against the readable canon.

## Next

- [Members, agents, hosts, placements](members-agents-hosts.md): the card types.
- [Write a canon](../guides/write-a-canon.md): a worked example.
- [Write a Process card](../guides/write-a-process.md): working rules and the
  executable `workflow:` block.
