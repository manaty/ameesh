---
type: Reference
title: "Questions ouvertes de conception"
description: "Ce qui attend une décision du propriétaire ou une étude, après la session du 2026-10-03."
status: draft
tags: [questions, decisions]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:30:00+02:00" }
---

# Attend le propriétaire

Aucune question n'attend le propriétaire au 2026-10-04 (Q1 à Q8 tranchées, voir
les [décisions](decisions/)).

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
