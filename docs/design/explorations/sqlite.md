---
type: Exploration
status: exploration
title: "Pilote SQLite pour un profil personnel"
---

# Pilote SQLite pour un profil personnel

## Problème

Un service Postgres peut alourdir une installation personnelle sur une seule machine. L’interface de stockage existe, mais les garanties de Postgres ne se transposent pas automatiquement à SQLite.

## Intuitions

Un profil local à un seul exécuteur pourrait remplacer certains réveils et verrous par des mécanismes locaux. La décision sur le stockage conditionne déjà cette piste à la confirmation du besoin.

## Questions ouvertes

- Quels usages personnels justifient un deuxième pilote ?
- Comment préserver les baux, les transactions et la consommation unique des reçus ?
- Quelles limites du profil doivent rester visibles quand plusieurs processus accèdent à la base ?

Contexte existant : [Décision sur le stockage et la piste personnelle](../decisions/0016-stockage.md), [Spécification de l’interface de stockage](../specification.md). Ces références éclairent la question ; elles ne promeuvent pas cette fiche en étude ou en travail décidé.
