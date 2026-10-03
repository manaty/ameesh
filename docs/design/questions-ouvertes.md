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
| Q6 | Application mobile native pour TPK : application ameesh générique (open source, éventuellement aux couleurs de TPK) ou intégrée aux applications mobiles existantes de TPK ? Pour les approbations seulement, ou aussi pour le travail quotidien (fils, notifications) ? | À instruire avec le propriétaire et TPK. |
| Q5 | L'orchestrateur Nexlink devient-il un agent à tours (réveillé par sa surveillance) pour pouvoir quitter le PC ? | Oui à terme ; c'est ce qui rend utile une deuxième machine. |

# Attend une étude ou un prototype

- Profil ameesh d'OKF Federation : types `Agent`, `Team`, `WorkPackage`,
  `Pipeline` ; liaison `human:<id>` → passkey / clé d'appareil ; `agent:<id>` → compte.
  Une partie pourrait remonter dans okf-federation.
- Isolation des agents (un utilisateur Unix ou un conteneur par agent) : coût,
  compatibilité avec les harnais et leurs sessions.
- File des décisions par humain (R17) : où vit-elle (Nexlink, ameesh, canon) ?
- Allocation des agents partagés entre projets : règle déclarative dans le canon.
- Analyse détaillée des projets homonymes (tlhc/amesh, samchung95/amesh,
  code-rabi/amesh) pour en reprendre les bonnes idées.
