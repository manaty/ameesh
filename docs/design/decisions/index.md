# Owner decisions

* [Name: ameesh, cart logo, manaty/ameesh repository](0001-nom-ameesh.md) - The agent-mesh package becomes ameesh; full renaming right away; private repository, then public at the first release.
* [Scope: coordinate mixed teams of humans + agents for all office work](0002-portee-equipes-mixtes.md) - ameesh is not only for software development: it coordinates humans and agents, on devices and servers, for any office trade.
* [ameesh is a standalone product, used by Nexlink and natively integrated](0003-ameesh-et-nexlink.md) - Split of roles between Nexlink (identities, pages, messages, human signatures) and ameesh (execution).
* [Open source alternatives and connectors to common tools](0004-open-source-et-outils-courants.md) - ameesh must run on an open source stack (even if that means creating the missing piece) and connect to common tools such as Slack.
* [The canon is in OKF in git, federated according to OKF Federation](0005-canon-okf.md) - Teams, responsibilities, project structure are described in OKF; Nexlink and the tools are only views or proposal interfaces onto it.
* [All exchanges are readable and auditable by humans](0006-lisibilite-humaine.md) - Agents talk to each other in human language, in channels the team can read; no hidden channel.
* [Integrations reuse the standards of agent tools](0007-standards-des-harnais.md) - No connector format specific to ameesh: MCP, skills, harness plugins.
* [Agents have a card in the OKF canon; their state stays in ameesh](0008-agents-dans-le-canon.md) - Declaration of the agent (sponsor, role, capabilities, harness, budget, tools) in the canon; runtime state in ameesh.
* [Every statement is signed; no interim procedure before ameesh v1](0009-paroles-signees.md) - Messages from humans and agents must be signed to serve as proof; our current sessions remain unsigned.
* [Human responsibility, living projects, multiple orchestrators](0010-responsabilite-et-orchestrateurs.md) - Every agent has a human in charge; a project can split; a human can have several orchestrators and a project several humans. mesh-design becomes the orchestrator of ameesh.
* [Hosting is a configuration, prepared by the user's harness](0011-hebergement-configurable.md) - Local, Nexlink page or provisioned VPS; installation driven by the harness the user already has.
* [Ed25519 ceremony dropped; authority through ameesh-approve](0012-autorite-par-ameesh-approve.md) - Passkey, Nexlink application or native application; a single receipt format.
* [ameesh licence: AGPL-3.0-only, with a contribution agreement](0013-licence-agpl.md) - Terms frozen at v3; CLA for external contributions.
* [Turn-based orchestrators; placement decided by the humans in charge](0014-orchestrateurs-a-tours-et-placement.md) - Interactive attachment; per-host policy; API key or plan chosen by the person in charge.
* [No named client; public interfaces to build the front ends](0015-interfaces-des-facades.md) - Third-party applications, including native mobile ones, without imposing Nexlink.
* [Storage: Postgres in v1, SQL behind an interface, SQLite later](0016-stockage.md) - Postgres installed locally by the installer; SQLite driver for personal mode afterwards.
* [ameesh-approve reachable through a Nexlink page](0017-approbation-via-page-nexlink.md) - HTTPS exposure of the approval service for the switchover.
* [Agent speed](0018-vitesse-des-agents.md) - Review by risk class, interruption, session rotation, measured lead times.
* [Budgets and routing](0019-budgets-et-routage.md) - Model and effort per task, caps, plan gauges read at the source.
* [Model catalogue and evaluation](0020-catalogue-et-evaluation-des-modeles.md) - List, discover and evaluate models to guide model/effort routing.
* [Harness catalogue](0021-catalogue-des-harnais.md) - Harnesses described, certified and evaluated by a central service instead of being hard-coded.
* [Bug self-repair](0022-auto-reparation.md) - Signals, triage in the canon, repair, verification, delivery, monitoring.
* [Adoption criterion for settings](0023-critere-d-adoption-des-reglages.md) - Significant gain in lead time or cost, with no loss of overall quality after review and acceptance testing.
* [Real-time progress view](0024-vue-temps-reel.md) - Timeline of lots, agents, milestones and budget, fed by ameesh events.
* [A fresh session per lot](0025-une-session-par-lot.md) - Build and review: fresh session per lot, kept until the merge; orchestrators rotated with a summary.
* [ameesh-approve hosting per team](0026-hebergement-d-ameesh-approve-par-equipe.md) - PC + relay, Nexlink page or own server; RP ID exactly equal to the team's host.
* [Automatic switch between accounts](0027-bascule-automatique-entre-comptes.md) - Secondary account when the primary nears its limit, return after the reset.
* [Host resources and distribution](0028-ressources-des-hotes-et-repartition.md) - Measurement, back-pressure, orphaned resources, moving agents between admitted hosts.
* [Persona and session](0029-persona-et-session.md) - Durable identity and memory separate from executions; digital twin of humans; the canon declares placement rules, not the position.
* [No work without a possible wake-up](0030-pas-de-travail-sans-reveil-possible.md) - Guarded assignment, adoption and resumption by ameesh, pushed liveness alerts, delegation with a deadline, identity never derived from the directory.
* [Several canons on the same host](0031-plusieurs-canons.md) - List of canons, identity by federation, synchronisation confined to the canon, names to the first declarer; lots L42–L45, L47.
* [Persona, roles and sessions](0032-persona-roles-and-sessions.md) - Distinct identity, function and execution; persona memory and off-device session backups; v1 agents live until their lot ends; one mesh per organisation.
* [Isolated meshes, hosting, identity and human roles](0033-isolated-meshes-hosting-and-identity.md) - Separate organisations, organisation-owned server accounts, generic OIDC, full substitution by deputies, common roles and visibility for responsible humans.
* [Consume first what expires](0034-consommer-d-abord-ce-qui-expire.md) - Plan accounts form a pool: each new session takes the account furthest behind its pace (amendment of 2026-10-10), on a tie the one whose unused capacity expires soonest, under the allowed pace; amends 0027.
* [Resident agent](0035-agent-resident.md) - You talk to an agent, you do not take it: a resident harness process under a lease when the harness allows it, ameesh chat then the Nexlink page, human messages served at the end of the turn and !urgent at the safe point, attach becomes the exception; lots L75–L83.
* [Internal auditor](0036-auditeur-interne.md) - A persona, on the most capable model the organisation assigns it and at maximum effort (amendment of 2026-10-11), checks every hour and at each urgent alert the agents, plans, spending, machines, CI and lots, and analyses every night and after each incident the time losses down to their root cause; fixes, merges and deploys ameesh bugs on its own, CI green; acts alone on what is reversible, proposes the rest.
* [Never stopped](0037-jamais-a-l-arret.md) - Continuous improvement backlog (lots with an expected value) picked up automatically by idle agents when no lot is waiting for them; never an irreversible or production action; alert when the backlog is empty.
