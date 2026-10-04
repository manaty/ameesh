---
type: Decision
title: "Portée : coordonner des équipes mixtes humains + agents pour tout travail de bureau"
description: "ameesh ne sert pas qu'au développement logiciel : il coordonne humains et agents, sur appareils et serveurs, pour tout métier de bureau."
status: stable
tags: [portee, metiers]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

La v1 a été conçue pour le chantier Nexlink : des agents de code dans des worktrees git, un orchestrateur, un board.

# Décision

ameesh coordonne le travail d'**équipes mixtes d'humains et d'agents IA**, tournant sur des **appareils** (téléphones, PC) et des **serveurs**. Le développement logiciel est **un cas parmi d'autres**. Cas cibles cités par le propriétaire : une **équipe de SAV** qui répond aux mails entrants ; des **coordinateurs de transport** (enlèvements, réception, emballage, expédition) ; plus généralement **tout métier où l'humain n'utilise qu'un ordinateur**.

# Conséquences

- Les notions propres au dev se généralisent : agents → membres (humains ou agents) ; worktree → espace de travail ; cycle intake→build→qa→merged→promoted → un modèle de pipeline parmi d'autres ; gel/revue/fusion → livrable/revue/décision.
- Nouvelles interfaces : canal entrant, canal sortant, outils métier, action proposée signée, politique d'autonomie (voir [l'étude métiers](../etudes/metiers-sav-transport.md)).
