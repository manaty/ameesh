---
type: Decision
title: "v2 : meshes étanches par organisation, hébergement, identité et rôles des humains"
description: "Une persona ne travaille jamais pour une autre organisation ; chaque organisation héberge son mesh (son infrastructure ou une VM) ; personas des serveurs sur clés API de l'organisation ; fournisseur d'identité OIDC générique choisi par l'organisation ; rôles des humains déclarés dans le canon OKF de l'organisation ; responsable ou suppléant peut tout faire à la place du responsable ; socle de rôles générique à tout métier intellectuel."
status: stable
tags: [v2, mesh, organisation, hebergement, identite, oidc, role, suppleance]
decided_by: human:smichea
decision_date: 2026-10-09
attestation: "réponses du propriétaire dans sa conversation avec mesh-design (2026-10-09) ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T06:15:00+02:00" }
sources:
  - { resource: "0031-plusieurs-canons.md", title: "Décision 0031 (plusieurs canons)" }
  - { resource: "0032-persona-roles-et-sessions.md", title: "Décision 0032 (persona, rôles, sessions)" }
  - { resource: "../etudes/ameesh-v2.md", title: "Étude ameesh v2" }
---

# Décision

1. **Étanchéité entre organisations.** Une persona d'une organisation ne
   travaille **jamais** pour une autre organisation, et inversement. Il n'y a
   ni invitation d'une persona dans le mesh d'une autre organisation, ni
   passerelle entre meshes. Un appareil qui sert deux organisations y fait
   tourner **deux meshes séparés** : deux bases, deux jeux d'exécuteurs, deux
   jeux de comptes et de secrets. Aucun état n'est partagé.
2. **Hébergement choisi par l'organisation.** Chaque organisation héberge son
   mesh : sur son infrastructure existante, ou sur une VM. Pour Manaty, c'est
   une VM chez son hébergeur. Le premier profil serveur est une **VM avec
   systemd** ; le profil cluster attend plusieurs serveurs.
3. **Les comptes des modèles appartiennent à l'organisation.** Les personas
   des serveurs utilisent des **clés API de l'organisation**, payées au jeton,
   sous les plafonds d'ameesh. Les abonnements des humains restent sur leurs
   appareils.
4. **Fournisseur d'identité OIDC générique, choisi par l'organisation.** Par
   exemple Google, restreint au domaine d'e-mail de l'organisation. Les essais
   se font avec Keycloak et Supabase Auth. Nexlink doit être compatible : c'est
   un ticket du dépôt Nexlink. ameesh ne dépend pas de Nexlink pour
   l'identité (0015).
5. **Ordre.** On commence par le premier serveur et la base partagée (phase 3
   de l'étude), avec en parallèle la séparation entre persona et session, la
   mémoire de persona et la sauvegarde des sessions (phase 1). Les rôles
   suivent (phase 2).
6. **Rôles des humains.** Ils sont déclarés dans le **canon OKF de
   l'organisation** (son dépôt `home`), comme ceux des personas.
7. **Suppléance complète.** Le responsable humain d'une persona, ou l'un de ses
   suppléants, peut faire **tout** ce que le responsable peut faire, y compris
   valider et approuver, pour ne jamais bloquer une décision. Chacun agit sous
   sa propre identité : un suppléant approuve avec **sa** passkey (0012).
   L'action est tracée comme faite par le suppléant, pour le responsable.
8. **Socle commun de rôles, générique à tout métier intellectuel**, pas
   seulement au développement logiciel. Chaque canon le complète par ses rôles
   propres. Proposition de socle, à valider :
   - **coordinateur** : répartit et suit le travail ;
   - **auteur** : produit un livrable ;
   - **relecteur** : vérifie le livrable d'un autre ;
   - **approbateur** : décide et engage l'organisation ;
   - **référent** : fait autorité sur un domaine ;
   - **veilleur** : surveille des sources et signale.

# Conséquences

- **0031 est restreinte par cette décision.** Plusieurs canons sur un même
  hôte restent possibles, mais **un mesh n'a qu'une organisation**. Un hôte
  qui en sert deux doit séparer leurs meshes. La configuration actuelle, une
  base partagée par les canons de deux organisations sur un même poste, est
  une étape transitoire de la v1, à scinder en v2.
- Le profil du canon gagne les rôles des membres humains, leurs suppléants et
  leurs supérieurs.
- L'étude v2 est mise à jour : la coopération entre meshes est retirée, et le
  volet D suit cette décision.
