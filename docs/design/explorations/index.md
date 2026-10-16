---
type: Index
status: exploration
title: "Explorations"
---

# Explorations

An exploration keeps an idea off the roadmap. One file per idea, with
`type: Exploration` and `status: exploration`, describes only the problem,
the intuitions and the open questions. Links to existing studies, decisions
or documents provide context.

These cards carry no proposed lots, no dates, no estimates and no "done"
criterion. No agent derives a lot, a WorkPackage, a backlog entry or a
roadmap line from them. Promotion to a [study](../etudes/) and then to lots
requires an explicit owner decision, recorded in [decisions/](../decisions/);
an exploration alone is not an authorization. A plan, roadmap or lot card
then references the decided study, never this reserve of ideas.

The common rule is published in the
[Process card, §4](https://github.com/manaty/home/blob/main/processus/lots.md#4-files-de-travail),
after [home#52](https://github.com/manaty/home/pull/52) merges.

- [Provisioning a hosted ameesh-approve](ameesh-approve-heberge.md)
- [Model evaluation](evaluation-modeles.md)
- [Harness descriptors and conformance](descripteurs-harnais.md)
- [Signals and the self-repair loop](auto-reparation.md)
- [Additional transports for threads](transports.md)
- [Gate exposed as an MCP proxy](proxy-mcp.md)
- [SQLite driver for a personal profile](sqlite.md)
- [One Unix user per agent](uid-par-agent.md)
- [Installer driven by the user's harness](installeur.md)

The `tests.test_l1124_explorations` guard checks the cards and the local
references from plans, roadmaps and lots. It is part of the documentation
readers in CI. Lots and plans of the external canon are not present in this
repository: their validation belongs to the canon.
