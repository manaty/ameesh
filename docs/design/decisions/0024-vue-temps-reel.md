---
type: Decision
title: "Une vue temps réel de l'avancement (type Gantt), rendu humain natif d'ameesh"
description: "Lots sur une frise (demande → gel → verdict → fusion), agents (modèle, effort, état, tâche), jalons et budget, alimentés par les événements d'ameesh ; dans Nexlink, rendu naturel de la page projet."
status: stable
tags: [observabilite, gantt, vue, nexlink]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "demande du propriétaire (05:47) transmise par l'orchestrateur Nexlink, rapportée par mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T05:50:00+02:00" }
---

# Décision

ameesh fournit une **vue temps réel de l'avancement du projet**, du genre Gantt,
lisible sur téléphone :

1. les **lots** sur une frise (demande → gel → verdict → fusion), leur état
   (actif, en revue, bloqué, approuvé, fusionné) et leur nombre de blocages ;
2. les **agents** : modèle, effort, état (travaille, repos, pause), durée et
   tâche en cours ;
3. les **jalons** (production, stores, recette…) ;
4. le **budget** : dépense réelle des usages payés au token, jauges des forfaits.

# Conséquences

- C'est la vue humaine native de `ameesh list` et du cycle de vie des lots,
  **alimentée par les événements d'ameesh** (transitions de lots, tours, baux,
  coûts, verdicts de revue) plutôt que reconstruite depuis git et le board.
- Dans Nexlink, la page projet en est le rendu naturel (vue du canon OKF plus
  état d'exécution) ; une façade tierce peut la construire sur les mêmes
  interfaces ([0015](0015-interfaces-des-facades.md)).
- Une référence existe pour Nexlink (page de suivi construite par
  l'orchestrateur sur ses propres données) : elle sert de maquette.
- Lot L24 : export JSON des événements et de l'état (`ameesh progress --json`),
  modèle de données de la frise, et une vue de référence.
