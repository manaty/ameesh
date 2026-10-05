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
  max_agents: 2
---
```

The host name is what the runner of that machine uses (`AMEESH_HOST`, else the
machine's host name).

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
host: banc
credential_mode: api-key
cwd: /srv/acme/ouvrier
---
```

Exactly one placement per agent. Secrets (API keys, subscription tokens) are
**never** in the canon: the placement only says which credential mode is used;
the secret is installed on the host.

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
