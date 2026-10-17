---
type: Exploration
status: exploration
title: "Signaux et boucle d’auto-réparation"
---

# Signaux et boucle d’auto-réparation

## Problème

Un signal d’erreur peut être dupliqué, mal interprété ou disparaître avant que sa cause soit établie. Une réparation automatique peut amplifier ce défaut si elle transforme un signal en autorisation.

## Intuitions

Reproduire le défaut avant de modifier, corréler les signaux et borner les tentatives pourrait rendre une boucle de réparation observable. Les limites du diff et l’escalade humaine pourraient rester explicites.

## Questions ouvertes

- Comment distinguer un défaut confirmé d’une demande d’évolution ?
- Quels signaux prouvent que la cause est corrigée plutôt que masquée ?
- Quand arrêter la boucle et transmettre les preuves à un humain ?

Contexte existant : [Étude de la boucle d’auto-réparation](../etudes/boucle-auto-reparation.md). Ces références éclairent la question ; elles ne promeuvent pas cette fiche en étude ou en travail décidé.
