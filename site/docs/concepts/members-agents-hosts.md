# Members, agents, hosts, placements

The ameesh profile of OKF defines four card types. All are ordinary OKF files;
the file name is free. The examples below come from the fictitious canon in
[`examples/canon/`](https://github.com/manaty/ameesh/tree/main/examples/canon).

## `Member`: a human

```yaml
type: Member
title: alice
roles: [project-lead, reviewer]
authenticators: []        # enrolled passkeys, added by reviewed pull request
```

`authenticators` lists the public keys a human may sign approvals with (see
[Receipts and ameesh-approve](receipts-and-approve.md)). Adding or removing one
is a reviewed change to the canon.

## `Agent`

```yaml
type: Agent
title: orchestre                      # the agent's name
responsible: human:alice              # REQUIRED
team: acme-web                        # team or project; also the project of its thread
projects: [acme-web, acme-api]        # optional (L243-1): the first is primary
capabilities: [read, report-drift, propose]   # `approve` is forbidden for agents
harness: claude                       # claude | codex | deepseek
model: claude-opus                    # optional
provider: anthropic                   # who bills the model
credential_mode: subscription         # api-key | subscription
budget_usd_per_day: 30                # optional
tools: [git, "mcp:transport-readonly"]
reviewers: [relecteur]                # optional
priority: 5                           # optional: paused last under critical host pressure (v1.3.0)
memory:                               # optional: the persona's memory repository (v1.3.0)
  mode: neutral
  repository: "git@forge.example:acme/persona-orchestre.git"
```

**Responsibility.** An agent without a `responsible` that resolves to a unique
human `Member` is **not claimable**. When a canon is configured,
`AMEESH_REQUIRE_RESPONSIBLE` defaults to on.

**No agent approves.** A card with `approve` in `capabilities` is a blocking
error: authority belongs to humans, and is proven by a receipt.

**Capabilities.** Known values (anything else raises the warning
`agent-capability-unknown`):

| capability | allows |
|---|---|
| `read` | read the canon, the threads and the repositories |
| `report-drift` | report a gap between the canon and reality |
| `propose` | propose a change (branch, PR, action proposal) |
| `integrate` | assemble others' work: rebase PRs, resolve conflicts, build an integration or release branch; never merges into the base branch |
| `merge` | merge an approved PR into the base branch once its required reviews and CI are green |
| `release` | tag, publish and deploy a release once the owner's go is recorded, following the release procedure |

None of them carries a decision: `merge` and `release` *execute* an approval
already given by a human (approved review, recorded go). They never replace
it. An ephemeral agent still gets only `read` and `propose`.

**Projects.** A card may list several `projects` (L243-1, migration 0388): the
**first is primary**, and the others are secondary — placement, lots and mail
use the primary unless the operation names another.

**Naming.** Since L243 the default name of a new agent is
`<org>-<repo>-<role>-<seq>` (or `<org>-<role>-<seq>` across projects), read
from the canon's `extensions.ameesh.naming` rules:

```bash
ameesh agent name --role R [--project P …] [--json]   # generate, without creating
ameesh agent spawn <name> --by CREATOR --ttl 2h       # create an ephemeral agent
```

Every card and every registry row reserves its number, so a name is never
reused — stopped agents included. A role without a short name is refused, two
repositories never share a segment and one repository keeps the same segment in
every canon (a divergent mesh is refused), and an unreadable naming block is
refused, never guessed.

## `Host`: a machine or a cluster

```yaml
type: Host
title: banc
responsible: human:alice      # REQUIRED
tags: [test]                  # optional: labels that admissions can target (v1.3.0)
admins: [human:bruno]         # optional: administrators, for the visibility rule (v1.3.0)
policy:
  harnesses: [deepseek, codex]          # absent = all
  providers: [deepseek, openai]
  credential_modes: [api-key]
  max_agents: 2
  resources:                            # optional pressure limits (v1.3.0)
    min_mem_available: 1GiB
    max_swap_used: 8GiB
    max_load: 24
    min_disk_free: 2GiB
    min_battery_percent: 25             # on battery: no new turn below (laptop host)
    stop_battery_percent: 10            # on battery: clean stop below
    max_cpu_pressure: 20%               # CPU pressure (PSI some, 60 s): no new turn above
  work_roots: {acme-web: /srv/acme/acme-web}   # working directory per team (v1.3.0)
  work_root: /srv                               # default: work_root/<team>
  work_dirs: {ouvrier: /srv/acme/ouvrier}       # per agent, wins over the above (v1.3.1)
```

The host's responsible human sets the rules of their machine, for example "only
DeepSeek and Codex agents, billed by API key". The host also decides **where**
agents work: the working directory is `policy.work_dirs[<agent>]`, else
`policy.work_roots[<team>]`, else `policy.work_root/<team>`, and each accepts
the `{agent}` template (`/srv/acme/acme-web-{agent}`: one worktree per agent).
See [Runner and leases](runner-and-leases.md#working-directory) for the
transitional fallback and what the runner does when the directory is missing,
and [host resources](runner-and-leases.md#host-resources-and-back-pressure) for
`policy.resources`. A host that lends its unused capacity to agents declares
its offer in `policy.lend` (CPUs, memory and disk at most, windows, the
application's thresholds at which agents give way); `canon check` validates
it, and an unreadable offer lends nothing. The contract shared with the
device runners is
[docs/GARDE-DE-CAPACITE.md](https://github.com/manaty/ameesh/blob/main/docs/GARDE-DE-CAPACITE.md).

## `Placement`: which agent may run where

Since v1.3.0 a `Placement` card is an **admission**: the hosts (by name or by
tag) where an agent is admitted, without a working directory. The current host
of the agent is execution state.

```yaml
type: Placement
title: orchestre@atelier
agent: orchestre
hosts: [atelier]              # admitted hosts, in order of preference
host_tags: [prod]             # or tags of admitted hosts
credential_mode: subscription
```

Older cards (a single `host:`, a `cwd:`) are still read: `host` is a single
admitted host. Their `cwd` is ignored when the host's policy gives a directory
(`admission-cwd-ignored`); when it does not, it is used as a **transitional
fallback** with the warning `admission-cwd-inherited`. That fallback is removed
in v1.5.0 at the latest: move working directories to `policy.work_dirs` /
`work_roots`.

**Governed placement.** The project's responsible human decides where an agent
runs, within the policy of the host's responsible human. For every canon agent
of a host, `canon sync` writes whether its placement on *that* host is
admitted. It is refused when the host policy rejects the agent's harness,
provider, model or credential mode, when the agent has no placement on this
host, when the placement is ambiguous, or when the `Host` card is missing. A
refused, missing or not-yet-evaluated verdict **closes new claims**; it never
interrupts a running lease.

A verdict holds only for the profile it judged (host, harness, provider, model,
credential mode). Any change made outside `canon sync` (registering the agent
by hand with another harness, an import, manual SQL) closes new claims until
the next sync. `canon sync` never moves an agent whose current host is still
admitted; ameesh moves an agent by itself only between two turns, to another
admitted host, when relocation under pressure is enabled
(`AMEESH_RELOCATE=1`).

**Visibility rule** (v1.3.0). When an agent declares a memory repository
(`memory.repository`), it runs on a host only if its responsible human and
the host's `admins` have access to that repository. `canon sync` checks it
through the forge (`AMEESH_FORGE`), caches the verdict briefly, and refuses the
placement otherwise (`persona-hidden-from-host`); `canon check` stays offline.

```bash
ameesh placement check [--agent A] [--json]   # read-only: current placements, why refused,
                                              # admissible hosts and modes; exit 1 if one is refused
```

## Ephemeral agents

```bash
ameesh agent spawn <name> --by <creator> --ttl 2h [--cwd DIR] [--prompt TEXT]
```

An ephemeral agent inherits its `responsible` from its creator at creation,
gets capabilities limited to `read` and `propose` (and to the creator's), and
has a mandatory expiry (at most 7 days, never beyond an ephemeral creator's).
Expired ephemeral agents are never claimable.

## Validation

`ameesh canon check` reports, as text or `--json` (code, severity, file,
explanation):

**Blocking errors**: an `Agent` without `responsible`, or with `approve` in
`capabilities`; a `responsible` that does not resolve to a human `Member`; a
`Placement` to an unknown agent or host, or without `hosts` nor `host_tags`; a
placement that violates the host policy; two placements for the same agent;
an unreadable `policy.resources`.

**Warnings**: a host without placement, an agent without placement, a review
policy declared without a `default` class, an admission tag that matches no
host (`admission-tag-unknown`), a placement `cwd` ignored or inherited
(`admission-cwd-ignored`, `admission-cwd-inherited`), an agent without a
working directory (`host-work-dir-missing`).

An error tied to an agent blocks that agent; an error tied to a host blocks the
agents placed there; any other error (federation, unreadable profile card)
blocks every agent of the host.
