---
type: Decision
title: "Budgets et routage : modèle et effort par tâche, plafonds, jauges de forfait lues à la source"
description: "L'orchestrateur choisit modèle et effort par tâche ; budgets par harnais, agent et projet au canon ; plafond horaire pour l'usage payé au token ; jamais de dépassement des forfaits, lus à leur source ; comptabilité par tour."
status: stable
tags: [budget, cout, forfait, routage, modele]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "décision du propriétaire transmise par l'orchestrateur Nexlink (message de 00:18), rapportée par mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:30:00+02:00" }
sources:
  - { resource: "~/.local/bin/nexlink-cost", title: "Implémentation de référence (comptabilité, budgets)" }
  - { resource: "~/.local/bin/nexlink-agent", title: "Implémentation de référence (set, budget_gate, interrupt)" }
---

# Décision

1. **Routage** : l'orchestrateur choisit et change le **modèle** et l'**effort**
   de chaque agent selon la tâche (modèle fort pour la sécurité et le natif,
   effort bas pour les textes et les tests) ; effet au tour suivant.
2. **Budgets** : pour l'usage **payé au token**, plafond par heure glissante
   (référence actuelle : 10 USD/h pour l'ensemble des agents DeepSeek). Pour les
   **forfaits** (Claude, Codex) : **ne jamais dépasser leurs limites**. Garde-fou
   de rythme : pause des agents d'un harnais si l'utilisation atteint
   `min(90 %, part écoulée de la fenêtre + 10 points)`, lue sur les **vraies
   jauges** (Claude : `rate_limit_event` du stream-json, fenêtres cinq heures
   et sept jours ; Codex : `rate_limits` primaire et secondaire des journaux de
   session).
3. **Comptabilité par tour** : différence du coût cumulé par session (Claude),
   de l'usage cumulé par fil (Codex), usage par étape × barème (DeepSeek) ;
   barème modifiable.
4. **L'orchestrateur et les humains comptent dans le même forfait** que les
   agents du même harnais (d'où la marge de 10 points).

# Conséquences

- Exigence R20 ; dans le canon : budgets par harnais, par agent et par projet,
  barème, règles de routage modèle/effort par classe de tâche et de risque.
- Dans ameesh : lecteurs de jauges et comptabilité par tour (lot L12, hors
  exécuteur) ; garde de budget avant chaque tour et application du modèle et
  de l'effort par harnais (lot L13, dans l'exécuteur, après L8).
- `ameesh cost` remplace `nexlink-agent cost` ; `ameesh set <agent>
  model=… effort=…` écrit l'état d'exécution (le canon porte les valeurs par
  défaut et les plafonds).
