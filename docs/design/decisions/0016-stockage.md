---
type: Decision
title: "Stockage : Postgres pour la v1, SQL derrière une interface, SQLite plus tard pour le mode perso"
description: "Postgres partout en v1, installé par l'installateur en local ; tout le SQL regroupé derrière une interface de stockage ; un pilote SQLite pour le profil local dans une version suivante."
status: stable
tags: [stockage, postgres, sqlite, hebergement]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:30:00+02:00" }
---

# Contexte

La base d'ameesh ne porte plus que l'état d'exécution (baux, tours, lots,
registre des approbations consommées) et la boîte aux lettres en mode
autonome ; elle suit le profil d'hébergement ([0011](0011-hebergement-configurable.md)).
Pour un projet perso en local, un Postgres est un service de plus à installer,
lancer et sauvegarder (mesuré sur le PC : ~7 Mo au repos, ~180 Mo après une
suite de tests, image `postgres:17` de 643 Mo). Le code v1 utilise environ 140
constructions propres à Postgres (LISTEN/NOTIFY, verrous de ligne, délais de
requête, `jsonb`, verrou consultatif des migrations).

# Décision

1. **v1 : Postgres partout.** En local, c'est l'**installateur** (piloté par le
   harnais de l'utilisateur) qui le met en place — conteneur ou paquet système.
2. **Tout le SQL est regroupé derrière une interface de stockage**, pour qu'un
   autre pilote puisse s'ajouter sans toucher au reste.
3. **SQLite pour le profil local (perso) dans une version suivante**, si le
   besoin se confirme.

# Conséquences

- Demande de changement à instruire avec la spec révisée : extraire les accès
  SQL dispersés (registre, boîte, autorité, lots, exécuteur) vers un module de
  stockage unique, avec des opérations nommées (prendre un bail, renouveler,
  déposer, consommer un nonce…) plutôt que des requêtes.
- En mode local mono-exécuteur, le pilote SQLite pourra remplacer LISTEN/NOTIFY
  par un réveil local et les verrous de ligne par des transactions simples ; la
  suite de tests devra tourner sur les deux pilotes.
