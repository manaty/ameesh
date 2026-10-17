---
type: Exploration
status: exploration
title: "Un utilisateur Unix par agent"
---

# Un utilisateur Unix par agent

## Problème

Des agents partageant un utilisateur système peuvent accéder aux mêmes fichiers et secrets. Une signature de message ne prouve pas une provenance fiable si les autres agents peuvent lire la clé.

## Intuitions

Des identités système séparées, des conteneurs ou une signature par l’exécuteur pourraient isoler les clés. Ces pistes demandent de distinguer provenance du message et autorité d’une décision humaine.

## Questions ouvertes

- Quelle isolation protège réellement une clé contre les autres agents ?
- Comment partager un dépôt de travail sans partager les secrets ?
- Comment conserver une provenance vérifiable lors d’un changement d’hôte ou de harnais ?

Contexte existant : [Décision sur les paroles signées](../decisions/0009-paroles-signees.md), [Étude de l’autorité humaine](../etudes/autorite-humaine.md). Ces références éclairent la question ; elles ne promeuvent pas cette fiche en étude ou en travail décidé.
