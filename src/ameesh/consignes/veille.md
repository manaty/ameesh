WATCH (sent by `ameesh watch`, text: src/ameesh/consignes/veille.md) — $agent: make a SHORT check, then ACT.

1. `ameesh projects` and `ameesh alerts`: an agent idle with no lot for more than 15 min → give it work or write to its orchestrator; a turn over 40 min or a giant session → `ameesh restart`; an agent blocked or stopped by mistake → `ameesh resume`.
2. An orchestrator stopped or paused → `ameesh resume <orchestrator>`.
3. Your lots: restart the interrupted ones. Merge first a PR with green CI that unblocks a request of the owner, before any new work (delivery rule 1, docs/ORCHESTRATEUR.md « Livraison et déploiements »).
4. A bug fix is merged and deployed as soon as its CI is green, with `deploy/mise-a-jour` (check, back up, migrate, install, restart outside a turn, verify, rollback ready); tell the owner and the agents concerned afterwards (rule 2).
5. Every other deployment (new feature, minor version, migration that changes behaviour) is PREPARED in full — tag set, `poste.sh verifier <REF>` and `vm.sh verifier <REF>` passed, rollback plan known — and proposed to the owner as the first point when they come back, with the choices (version alone or all of main, migrations, risks) (rule 3). Never just forbid deployments without preparing them.
6. Production actions of a project, spending and secrets stay the owner's decisions: nothing irreversible without them.

Note what you did in `ameesh fil`, then hand back.
