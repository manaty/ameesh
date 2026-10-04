---
type: Decision
title: "Orchestrateurs à tours ; placement des agents décidé par les humains responsables"
description: "Les orchestrateurs deviennent des agents à tours avec attachement interactif ; le responsable ameesh du projet, ou l'infra à qui il délègue, décide quel agent tourne où et avec quels identifiants, dans les limites posées par le responsable de chaque serveur."
status: stable
tags: [orchestrateur, placement, infrastructure, identifiants, supervision]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T01:10:00+02:00" }
---

# Contexte

Les orchestrateurs (Nexlink, ameesh) sont des sessions interactives collées au
PC : sans bail, sans relance fiable, leur surveillance meurt avec la session.
Les déplacer sur un serveur pose la question des identifiants (abonnement ou
clé d'API) et de qui décide où tourne quoi.

# Décision

- **Les orchestrateurs deviennent des agents à tours** à la bascule vers
  ameesh : surveillance en service séparé qui dépose ses événements dans leur
  boîte, réveil par l'exécuteur, échanges dans le fil du projet, et
  **attachement interactif** à la demande (`ameesh attach`) qui prend le bail
  et rend la main à l'exécuteur à la sortie. D'abord sur le PC, puis sur un
  serveur.
- **Le placement est une décision humaine** : c'est **le responsable ameesh du
  projet** — qui **peut déléguer à des responsables d'infrastructure** — qui
  décide **quel type d'agent tourne sur quel serveur**, et **avec une clé
  d'API ou un forfait**.
- **Le responsable d'un serveur peut imposer ses règles**, par exemple
  n'accepter que des agents DeepSeek ou Codex sur sa machine.
- **ameesh doit faciliter cette gestion et sa supervision.**

# Conséquences

- Nouveaux concepts du canon : **Hôte** (machine ou cluster, avec son humain
  responsable et sa **politique** : harnais, fournisseurs et modèles admis,
  modes d'identifiants admis — clé d'API, forfait —, quotas, contraintes de
  données) et **Placement** (agent → hôte, mode d'identifiants), modifiés par PR
  revue selon les rôles de la fédération (responsable du projet, responsables
  d'infra délégués, responsable de l'hôte).
- ameesh **refuse un placement qui viole la politique de l'hôte** et propose
  les placements admissibles ; il ne déplace jamais un agent de lui-même.
- Les **secrets** (clés d'API, jetons de forfait) ne sont jamais dans le canon :
  le canon dit quel mode est utilisé, ameesh aide à installer le secret sur
  l'hôte (trousseau, gestionnaire de secrets) et vérifie sa présence.
- **Supervision** : par hôte et par projet — agents placés, baux, santé, coûts
  par fournisseur et par mode, consommation des forfaits au regard de leurs
  limites (R7), alertes à l'humain responsable concerné.
