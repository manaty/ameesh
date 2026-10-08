---
type: Decision
title: "Plusieurs canons sur un même hôte"
description: "ameesh lit une liste de canons ; un canon est identifié par sa fédération ; chaque synchronisation ne touche que son canon ; noms globaux au premier déclarant ; humains et hôte décrits par chaque canon ; périmètre ameesh déclarable dans un canon partagé."
status: stable
tags: [canon, federation, multi-canon]
decided_by: human:smichea
decision_date: 2026-10-07
attestation: "besoin confirmé par le propriétaire (« oui très important ») dans sa conversation avec claude3 ; modalités tranchées par claude3 sur délégation explicite du propriétaire (« tranche pour moi », 2026-10-08), après une nuit d'exploitation sur deux canons ; non signée"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T15:40:00+02:00" }
---

# Décision

ameesh gère plusieurs canons simultanés (manaty et Acme). Les
modalités et le plan de lots L42–L45 sont dans l'étude
[plusieurs canons](../etudes/plusieurs-canons.md).

1. La configuration de l'hôte porte une liste de canons ; le premier est le
   canon par défaut.
2. Un canon est identifié par l'`id` de sa fédération. Les références, le
   registre, les paquets, l'état du canon et le journal des authentificateurs
   portent ce canon.
3. Chaque synchronisation ne touche que son canon, et la fermeture se fait par
   canon.
4. Les noms restent globaux : le premier canon qui déclare un nom le garde, et
   un nom libéré peut changer de canon.
5. Les humains se résolvent dans le canon de l'agent ; chaque canon décrit
   l'hôte qu'il utilise, et l'exécuteur applique les limites physiques les
   plus strictes.
6. Un canon partagé peut borner les fiches ameesh à un dossier
   (`extensions: {ameesh: {scope: …}}` dans `federation.yaml`, seule clé
   libre du schéma OKF Federation — forme corrigée par L47).

# Statut

Décision `stable` depuis le 2026-10-08. Les modalités (points 4 à 6) ont été
confirmées telles quelles : elles tournent en production sur deux canons depuis
le déploiement de L36–L47, sans conflit de nom ni débordement de
synchronisation.
