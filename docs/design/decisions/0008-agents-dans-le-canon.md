---
type: Decision
title: "Les agents ont une fiche dans le canon OKF ; leur état reste dans ameesh"
description: "Déclaration de l'agent (parrain, rôle, capacités, harnais, budget, outils) dans le canon ; état d'exécution dans ameesh."
status: stable
tags: [agents, canon]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

Aujourd'hui l'existence d'un agent tient à ses scripts de lancement et à des messages du board ; n'importe quel agent peut en lancer un autre.

# Décision

- Chaque agent a une **fiche** dans le canon OKF (type `Agent`) : humain responsable, équipe, rôle et capacités (`read`, `report-drift`, `propose`, jamais `approve`), harnais et modèle, machine prévue, budget, outils autorisés, relecteurs.
- La fiche ne change que par **PR revue**.
- **L'état** (session, bail, machine courante, statut, dépense, courrier) reste dans ameesh.
- Les **agents éphémères**, limités à la lecture et à la proposition, peuvent exister sans fiche ; ils ont quand même un humain responsable, **hérité de l'agent qui les a créés** et enregistré par ameesh à leur création (R14).

# Conséquences

- Personne ne crée un agent ni n'élargit ses droits sans revue.
- Le registre des clés publiques des membres peut vivre dans le canon, modifié par PR revue.
