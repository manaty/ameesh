---
type: Exploration
status: exploration
title: "Porte exposée comme proxy MCP"
---

# Porte exposée comme proxy MCP

## Problème

Les outils d’un agent peuvent exposer des actions qui demandent une approbation. Une façade MCP devant la porte pose la question de la conservation du contrat d’action et de la preuve signée.

## Intuitions

Un proxy pourrait présenter les outils tout en reliant chaque appel à une action durable et à sa politique. La description d’un outil et les arguments reçus pourraient rester distincts de l’autorisation d’exécuter.

## Questions ouvertes

- Comment lier les arguments exécutés au contenu approuvé ?
- Comment rendre un résultat inconnu et une reprise idempotente à un client MCP ?
- Comment exposer les capacités sans élargir les droits de l’agent ?

Contexte existant : [Spécification des actions et de la porte](../specification.md), [Étude de l’autorité humaine](../etudes/autorite-humaine.md). Ces références éclairent la question ; elles ne promeuvent pas cette fiche en étude ou en travail décidé.
