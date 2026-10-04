---
type: Decision
title: "Un réglage ne s'adopte que pour un gain significatif, sans perte de qualité globale"
description: "Critère d'adoption d'un modèle, d'un effort ou d'un processus par le service d'évaluation : gain significatif de délai ou de coût, qualité globale mesurée après revue et recette inchangée ou meilleure."
status: stable
tags: [evaluation, qualite, modeles, processus]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "critère du propriétaire transmis par l'orchestrateur Nexlink (message de 04:53), rapporté par mesh-design ; non signé"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T05:00:00+02:00" }
sources:
  - { resource: "../etudes/constats-modeles-2026-10.md", title: "Constats et premier rejeu isolé" }
---

# Décision

Un réglage de **modèle**, d'**effort** ou de **processus** ne s'adopte que s'il
apporte un **gain significatif** (délai ou coût), et **jamais au prix de la
qualité globale**, mesurée **après revue et recette**, qui doit rester optimale.

# Conséquences

- La métrique d'évaluation (lots L15 et L17) n'est pas « tests verts » mais :
  verdict du relecteur, défauts trouvés en revue et après fusion, nombre
  d'allers-retours bloqués, défauts en recette ; à côté du délai et du coût.
- Un rejeu ne compte que s'il est isolé de toute trace de la solution.
- Application immédiate : l'effort bas de DeepSeek pour l'implémentation est
  écarté pour l'instant (aucun gain de temps, un point de qualité perdu).
