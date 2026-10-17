---
type: Exploration
status: exploration
title: "Provisionnement d’ameesh-approve hébergé"
---

# Provisionnement d’ameesh-approve hébergé

## Problème

Les modes hébergés d’ameesh-approve demandent une installation cohérente par équipe, avec domaine, état et identité isolés. Le code de passerelle, le TLS local et le rôle de lecture seule existent déjà ; leur présence ne prouve pas que les offres hébergées sont déployées.

## Intuitions

Distinguer les primitives existantes du provisionnement côté hébergeur pourrait clarifier le choix entre relais depuis un PC, page dédiée et serveur propre. Une préparation réversible pourrait préserver le RP ID et laisser l’enrôlement à l’humain.

## Questions ouvertes

- Quel état doit appartenir à l’équipe plutôt qu’à son hébergeur ?
- Comment constater une installation incomplète et reprendre sans fusionner deux identités ?
- Quelles preuves permettent de distinguer code disponible et service réellement actif ?

Contexte existant : [Contrat des trois modes d’hébergement](../ameesh-approve-hebergement-equipes.md), [Étude du provisionnement des hôtes](../etudes/l232-autonomous-host-provisioning.md). Ces références éclairent la question ; elles ne promeuvent pas cette fiche en étude ou en travail décidé.
