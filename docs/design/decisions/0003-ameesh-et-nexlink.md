---
type: Decision
title: "ameesh est un produit autonome, utilisé par Nexlink et intégré nativement"
description: "Partage des rôles entre Nexlink (identités, pages, messages, signatures humaines) et ameesh (exécution)."
status: stable
tags: [nexlink, architecture]
decided_by: human:smichea
decision_date: 2026-10-03
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:00:00+02:00" }
---

# Contexte

La spec Nexlink agents-and-teams (AM1–AM15) prévoyait déjà comptes agents, conversations de chantier, signatures par clés d'appareil, relais et baux ; ameesh v1 a son propre registre, sa boîte, ses baux et ses signatures. Les deux se recouvrent.

# Décision

- ameesh est un **produit autonome** ; **Nexlink l'utilise** (les organisations de Nexlink font tourner leurs agents avec ameesh).
- **Intégration native** : Nexlink est un moyen de communication d'ameesh et affiche les projets, modules et lots.
- Partage : **Nexlink** = organisations, membres humains, comptes agents, conversations, notifications, signatures humaines sur appareil, audit ; **ameesh** = exécuteur par machine, adaptateurs des harnais, sessions, tours, baux d'exécution, budget, cycle de vie des lots, vérification de l'autorité.
- Les échanges agent↔agent passent **aussi** par la messagerie, visibles des humains, rangés en un fil par lot.

# Conséquences

- Le cœur d'ameesh expose des interfaces (annuaire, messagerie, catalogue du travail, autorité, baux) ; Nexlink est un connecteur, natif et prioritaire, pas une dépendance du cœur.
- Voir [0005](0005-canon-okf.md) : les pages Nexlink sont une vue du canon OKF.
