---
type: Reference
title: "Questions ouvertes de conception"
description: "Ce qui attend une décision du propriétaire ou une étude, après la session du 2026-10-03."
status: draft
tags: [questions, decisions]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
---

# Attend le propriétaire

| # | Question | Recommandation de mesh-design |
|---|---|---|
| Q1 (ex-M3) | Quelle base pour l'état d'exécution ? Elle suit désormais le profil d'hébergement ([0011](decisions/0011-hebergement-configurable.md)) ; reste à savoir si le profil local peut se contenter de SQLite (pas de LISTEN/NOTIFY). | Postgres partout pour la v1 ; SQLite à étudier pour le profil local. |
| Q8 | L'historique git du dépôt (privé) contient des mentions d'un client : avant le passage public, réécrire l'historique ou publier à partir d'un instantané sans historique ? | Publier un instantané propre (nouvel historique public) : pas de réécriture d'un historique partagé par les agents. |

# Attend une étude ou un prototype

- Licences : rédaction du CLA et titularité des contributions produites par
  des agents ; compatibilité, version par version, des dépendances distribuées
  et des intégrations avec l'AGPL-3.0-only ([0013](decisions/0013-licence-agpl.md)).

- Profil ameesh d'OKF Federation : types `Agent`, `Team`, `WorkPackage`,
  `Pipeline` ; liaison `human:<id>` → passkey / clé d'appareil ; `agent:<id>` → compte.
  Une partie pourrait remonter dans okf-federation.
- Isolation des agents (un utilisateur Unix ou un conteneur par agent) : coût,
  compatibilité avec les harnais et leurs sessions.
- File des décisions par humain (R17) : où vit-elle (Nexlink, ameesh, canon) ?
- Allocation des agents partagés entre projets : règle déclarative dans le canon.
- Analyse détaillée des projets homonymes (tlhc/amesh, samchung95/amesh,
  code-rabi/amesh) pour en reprendre les bonnes idées.
