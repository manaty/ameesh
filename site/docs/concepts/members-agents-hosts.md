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
capabilities: [read, report-drift, propose]   # `approve` is forbidden for agents
harness: claude                       # claude | codex | deepseek
model: claude-opus                    # optional
provider: anthropic                   # who bills the model
credential_mode: subscription         # api-key | subscription
budget_usd_per_day: 30                # optional
tools: [git, "mcp:transport-readonly"]
reviewers: [relecteur]                # optional
```

**Responsibility.** An agent without a `responsible` that resolves to a unique
human `Member` is **not claimable**. When a canon is configured,
`AMEESH_REQUIRE_RESPONSIBLE` defaults to on.

**No agent approves.** A card with `approve` in `capabilities` is a blocking
error: authority belongs to humans, and is proven by a receipt.

## `Host`: a machine or a cluster

```yaml
type: Host
title: banc
responsible: human:alice      # REQUIRED
policy:
  harnesses: [deepseek, codex]          # absent = all
  providers: [deepseek, openai]
  credential_modes: [api-key]
  max_agents: 2
```

The host's responsible human sets the rules of their machine, for example "only
DeepSeek and Codex agents, billed by API key".

## `Placement`: which agent runs where

```yaml
type: Placement
title: orchestre@atelier
agent: orchestre
host: atelier
credential_mode: subscription
cwd: ~/acme/acme-web          # working directory on the host
```

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
the next sync. ameesh never moves an agent by itself.

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
`Placement` to an unknown agent or host; a placement that violates the host
policy; two placements for the same agent.

**Warnings**: a host without placement, an agent without placement, a review
policy declared without a `default` class.

An error tied to an agent blocks that agent; an error tied to a host blocks the
agents placed there; any other error (federation, unreadable profile card)
blocks every agent of the host.
