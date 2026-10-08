# Write a canon

This guide walks through the small canon of a fictitious organisation,
`acme`, checks it, and syncs it into the registry of one host. The files ship
as is in the repository under
[`examples/canon/`](https://github.com/manaty/ameesh/tree/main/examples/canon).
Card types are described in
[Members, agents, hosts, placements](../concepts/members-agents-hosts.md).

## 1. Layout

Any layout works; ameesh reads the `type` in each file's frontmatter. The
example uses one folder per kind:

```
examples/canon/
├── index.md                          # type: Index
├── log.md                            # type: Log
├── membres/alice.md                  # type: Member
├── membres/bruno.md
├── hotes/atelier.md                  # type: Host
├── hotes/banc.md
├── agents/orchestre.md               # type: Agent
├── agents/relecteur.md
├── agents/ouvrier.md
├── placements/orchestre-atelier.md   # type: Placement
├── placements/relecteur-atelier.md
├── placements/ouvrier-banc.md
└── decisions/0001-placement-du-banc.md   # type: Decision
```

`index.md`, `log.md` and the decision are ordinary OKF files of other types:
ameesh ignores them, so a canon can carry more than ameesh needs. The example
has no `federation.yaml`; add one when you need federation members or review
policies (section 6).

## 2. Humans

```yaml title="membres/alice.md"
---
type: Member
title: alice
description: "Responsable du projet acme-web (fictive)."
roles: [project-lead, reviewer]
authenticators: []
---
```

`authenticators` stays empty until the human enrols a passkey through
ameesh-approve; the enrolment produces an entry that is added here by pull
request.

## 3. Hosts and their policies

```yaml title="hotes/banc.md"
---
type: Host
title: banc
description: "Serveur de test : DeepSeek et Codex seulement, clé d'API seulement."
responsible: human:alice
policy:
  harnesses:
    - deepseek
    - codex
  providers: [deepseek, openai]
  credential_modes: [api-key]
  work_roots: {acme-web: /srv/acme/acme-web}
  max_agents: 2
---
```

The host name is what the runner of that machine uses (`AMEESH_HOST`, else the
machine's host name).

The host also says **where agents work**: `policy.work_roots` gives a working
directory per team (here every `acme-web` agent on `banc` works in
`/srv/acme/acme-web`). Since v1.3.1, `policy.work_dirs` names a directory per
agent, and the `{agent}` template gives one worktree per agent:

```yaml
policy:
  work_roots: {acme-web: "/srv/acme/acme-web-{agent}"}   # /srv/acme/acme-web-ouvrier, …
  work_dirs: {relecteur: /srv/acme/review}               # a named exception
```

The first path found wins: `work_dirs[<agent>]`, then `work_roots[<team>]`,
then `work_root/<team>`. Optional `policy.resources` limits (memory, swap,
load, disk) are described in
[Runner and leases](../concepts/runner-and-leases.md#host-resources-and-back-pressure).

## 4. Agents

```yaml title="agents/ouvrier.md"
---
type: Agent
title: ouvrier
description: >
  Agent de construction des lots
  d'acme-web.
responsible: human:bruno
team: acme-web
capabilities: [read, propose]
harness: deepseek
model: deepseek-chat
provider: deepseek
credential_mode: api-key
---
```

Every agent needs a `responsible` human, and none may have `approve`.

## 5. Placements

```yaml title="placements/ouvrier-banc.md"
---
type: Placement
title: ouvrier@banc
agent: ouvrier
hosts: [banc]
credential_mode: api-key
---
```

Exactly one placement per agent. A placement is an **admission**: `hosts`
lists the hosts where the agent may run, in order of preference (or
`host_tags` names host tags); it carries no working directory, which comes
from the host. Secrets (API keys, subscription tokens) are **never** in the
canon: the placement only says which credential mode is used; the secret is
installed on the host.

!!! note "Older placements with `host:` and `cwd:`"
    A card with a single `host:` is still read. Its `cwd:` is ignored when the
    host's policy gives a directory (`admission-cwd-ignored`); otherwise it is
    used as a transitional fallback, with the warning
    `admission-cwd-inherited`. That fallback is removed in v1.5.0 at the
    latest: move the directory to the host's `work_dirs` or `work_roots`.

## 6. Review policies (optional)

Review classes by file scope live in `federation.yaml`, which the example
does not include; this one declares the canon as a single-member federation
(see [Review classes](review-classes.md)):

```yaml title="federation.yaml"
federation: "0.1"
id: acme
root: acme
members:
  - id: acme
    ref: main
    bundle: .
review_policies:
  self_approval: forbidden
  risk_classes:
    default: normal
    rules:
      - paths: ["**/*.sql", "migrations/**"]
        class: sensitive
      - paths: ["docs/**"]
        class: light
```

## 7. Commit, push, check

ameesh reads the canon only from the merged canonical branch, through git
objects. Push it to a remote, then fetch:

```bash
mkdir acme && cp -r examples/canon/. acme/ && cd acme   # plus your federation.yaml, if any
git init -b main && git add -A && git commit -m "acme canon"
git remote add origin <canon repository> && git push -u origin main && git fetch origin

export AMEESH_CANON=$PWD          # or "canon" in the configuration file
export AMEESH_HOST=atelier        # the host this runner serves
ameesh canon check                # 0 errors expected; reads origin/main
ameesh placement check            # admitted and refused placements, and why
```

`placement check` also lists, for each agent, the **admissible** hosts and
modes, for example that `orchestre` (Claude, Anthropic) is refused on `banc`
because that host only admits DeepSeek and Codex.

To prototype without a git remote, `AMEESH_CANON_UNTRUSTED=1 ameesh canon show`
reads a plain directory; every finding is then tagged as not approved, and it
must never be used for real agents.

## 8. Sync into the registry

```bash
ameesh canon sync --bootstrap-ref main   # first sync of this database only
ameesh canon sync                         # afterwards (the runner also does it periodically)
ameesh list                               # canon agents: host, responsible, no lease yet
```

`--bootstrap-ref` names the trusted canonical branch for the passkey registry
the very first time; it is logged and ignored afterwards. A host can instead
fix it in its configuration (`AMEESH_CANON_REF=origin/main`).

## 9. Change it like code

Every later change (a new agent, a passkey, a moved placement, a stricter host
policy) is a pull request, reviewed according to the federation's policy, then
`ameesh canon sync --fetch` on each host. A pull request that is not merged has
no effect.

## 10. A second canon on the same host

Since v1.4.0 a host can also run agents declared in another organisation's
canon, next to its own (see
[The canon](../concepts/canon.md#several-canons-on-one-host)). Say a partner
keeps its canon in `~/development/partner/home`, a federation whose `id` is
`partner`, shared with other tools.

1. **ameesh cards in the second canon**, by a reviewed and merged pull request
   on that canon:
    - in `federation.yaml`, `extensions: {ameesh: {scope: ameesh}}`, so that
      ameesh reads only the `ameesh/` folder and ignores other files of the same
      `type`;
    - a `Member` card for each human responsible there (humans are resolved in
      the agent's own canon);
    - a `Host` card for this host: that canon's policy here (harnesses,
      providers, modes, `work_dirs` of its agents). Give it the **same
      `max_agents`** as the first canon's card: the smallest one caps the
      whole runner;
    - an `Agent` and a `Placement` card for each of its agents.

    Check the branch before merging:
    `ameesh canon check --canon <checkout of the branch> --ref HEAD` (0 errors).

2. **Host configuration**: turn `canon` into a list, keeping the `acme` canon
   **first** (it stays the default canon; do not reorder in the same step):

    ```json
    "canons": [
      {"path": "~/canon", "ref": "origin/main"},
      {"path": "~/development/partner/home", "ref": "origin/main"}
    ]
    ```

    The second `ref` is its trusted branch for passkeys; without it, bootstrap
    it once with `ameesh canon sync --canon partner --bootstrap-ref main`.

3. **Check**, before starting anything:

    ```bash
    ameesh canon check                        # one block per canon, both valid
    ameesh canon sync --fetch                 # each canon touches only its own rows
    ameesh placement check --canon partner     # the partner's agents admitted here
    ameesh hosts                              # where each physical limit comes from
    ```

4. **Agents**: `ameesh resume <agent>` (or `ameesh adopt` for an existing
   interactive session) to put them in service under the runner.

To move an agent from one canon to the other, remove its card from the old
canon first (the agent is stopped, its name freed), then declare it in the new
one. In the other order the second canon gets `canon-name-conflict` and writes
nothing.

**Rollback**: remove the second canon from `canons`. Its agents are no longer
claimable, nothing is stopped or deleted, and the first canon is untouched.
