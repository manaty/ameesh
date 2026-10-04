---
type: Decision
title: "L'hébergement est une configuration, préparée par le harnais de l'utilisateur"
description: "Pas de machine imposée : local, page Nexlink ou VPS provisionné ; l'installation est conduite par le harnais d'agent que l'utilisateur a déjà."
status: stable
tags: [hebergement, installation, provisionnement]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:40:00+02:00" }
---

# Contexte

La question ouverte M4 (« première machine en plus du PC ») supposait un choix
d'infrastructure fait une fois pour toutes.

# Décision

- **Où tourne ameesh est une configuration, par projet**, pas une décision
  d'architecture. Profils possibles : **local** (projet personnel, une
  machine), **page Nexlink** (hébergée par un serveur de page), **VPS
  provisionné** (proposé et créé par du code d'infrastructure), et par
  extension un serveur ou un cluster existant.
- **La personne qui lance ameesh en local a déjà un harnais d'agent** (Claude
  Code, Codex, DeepSeek Harness…) : on l'utilise **tout de suite** pour un
  **provisionnement intelligent** — il demande à l'utilisateur où il veut
  héberger, ou s'il reste en local pour un projet perso, puis prépare la
  configuration.

# Conséquences

- ameesh se distribue aussi comme **skill / plugin d'installation** au
  standard des harnais ([0007](0007-standards-des-harnais.md)) : entretien avec
  l'utilisateur, choix du profil, amorçage du canon (racine OKF, fédération,
  fiches des premiers agents), déploiement de l'exécuteur et de la base,
  enrôlement de la passkey de l'utilisateur dans ameesh-approve.
- Provisionnement d'un VPS par **OpenTofu** (MPL-2.0, Linux Foundation) plutôt
  que Terraform (BSL depuis 2023) pour rester open source.
- **Toute dépense** (VPS, domaine) est une action coûteuse : elle passe par une
  approbation signée de l'utilisateur — la première cérémonie d'ameesh-approve
  sert à approuver sa propre infrastructure.
- La question de la base (ex-M3) suit le profil : elle vit là où tourne
  l'exécuteur, sauvegardée hors de la machine dès qu'on quitte le profil local.
