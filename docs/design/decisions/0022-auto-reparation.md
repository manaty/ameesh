---
type: Decision
title: "ameesh détecte et répare automatiquement les bugs remontés par les interactions"
description: "Boucle complète signaux → triage → réparation → vérification → livraison → surveillance, gouvernée par une politique d'auto-réparation au canon, sans approbation implicite pour l'irréversible."
status: stable
tags: [auto-reparation, signaux, triage, gouvernance]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T03:05:00+02:00" }
sources:
  - { resource: "../etudes/boucle-auto-reparation.md", title: "Étude de la boucle d'auto-réparation" }
---

# Décision

ameesh doit **détecter et réparer automatiquement les bugs remontés par les
interactions**, sur le modèle d'un orchestrateur de tickets existant en
production, en reprenant ce qui marche et en corrigeant ses limites
([étude](../etudes/boucle-auto-reparation.md)).

# Conséquences

- Boucle : **signaux** (erreurs, alertes, conversations, support, retours
  d'utilisateurs) → **classification et déduplication** → **triage** selon la
  **politique d'auto-réparation du canon** (par équipe : sources, natures,
  gravités et chemins réparables seuls, budgets, interrupteur durable) →
  **réparation** par un agent (reproduire d'abord, plus petit correctif, tests,
  rapport, gardes sur le diff) → **vérification** avec preuves → **livraison**
  par la porte (la fusion est une action ; la promotion en production reste
  humaine par défaut) → **surveillance après livraison** et **réouverture sur
  récidive** → notification du demandeur.
- Différences voulues avec le modèle étudié : toute décision humaine passe par
  un message signé ou un reçu, **jamais par du texte libre ni une réaction** ;
  **aucune approbation implicite par le silence pour une action irréversible
  ou coûteuse** (l'escalade change de destinataire, elle ne décide pas) ;
  transitions journalisées ; état durable et plusieurs exécuteurs (baux) ;
  interrupteurs au canon.
- Les évolutions ne sont jamais implémentées automatiquement sans décision
  humaine ; les corrections de données sont proposées, jamais exécutées seules.
- Exigence R23 ; lots L18 à L23 (spécification §13).
