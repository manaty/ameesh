---
type: Study
title: "Constats sur les modèles et l'effort par type de lot (nuit du 3 au 4 octobre 2026)"
description: "Premiers constats de l'orchestrateur Nexlink, à reprendre comme tâches de référence pour l'évaluation des modèles et des harnais."
status: draft
tags: [modeles, effort, evaluation, constats]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T03:15:00+02:00" }
sources:
  - { resource: "message de l'orchestrateur Nexlink, 2026-10-04 02:38", title: "Constats transmis à mesh-design" }
stale_after: 2026-11-04
---

# Constats (observations, non mesurées)

1. **Implémentation, DeepSeek « flash » en effort maximal** : bonne qualité,
   mais tours longs et sessions qui saturent vite (après 5 ou 6 lots). Passé en
   effort élevé pour l'implémentation courante ; maximal gardé pour le Rust natif.
2. **Relecture sensible, Codex en effort moyen** (SQL, sécurité, natif) : très
   efficace pour trouver de vrais défauts par sondes indépendantes (29 blocages
   en 24 h, presque tous fondés), mais bloque aussi sur des détails (corrigé par
   la revue proportionnée, [0018](../decisions/0018-vitesse-des-agents.md)).
   Effort bas pour les relectures légères (docs, scripts).
3. **Claude Opus** : revues SQL et sécurité, pilotage de recette ; **Sonnet en
   effort moyen** pour le design et la relecture visuelle (à évaluer).
4. **Lots de style ou d'UI** : la plus grosse perte est le relais designer →
   implémenteur → relecteur ; le designer qui modifie lui-même va plus vite.
5. **Les erreurs coûteuses viennent surtout des sessions longues** (contexte
   compacté, consignes ratées), plus que du choix du modèle.

# Tâches de référence suggérées

Petites retouches d'UI avec capture ; correctifs SQL avec sondes de privilèges ;
défauts natifs reproduits sur banc ; relectures avec contre-exemples connus
(les blocages de revue de la nuit, avec leur sonde, en sont une source prête).

# Mesures et leçons (03:28)

- Après les règles de vitesse : délai travail → fusion maximal passé de 100 min
  (fenêtre 01:25–02:25, 8 fusions) à 21 min (02:25–03:25, 6 fusions) ; petit
  échantillon. Ce qui a porté : revue proportionnée, rotation des sessions
  saturées avec résumé, pouls automatique (un tour de 67 min passé à attendre
  une suite complète), mode rapide des relecteurs.
- **Rejeu d'évaluation contaminé** : un rejeu en DeepSeek flash effort bas
  (6,5 min, ≈ 0,15 $, tests verts, note 9/10) avait accès à la solution de
  référence par le worktree (diff identique). Exigence : rejouer dans un dépôt
  **isolé** (archive git + init, sans historique ni remote), sans accès au
  board ni aux autres worktrees.

# Premier rejeu isolé (03:46)

Lot de référence : correctif mobile JS d'environ 400 lignes avec tests, rejoué
dans un dépôt isolé.

| Configuration | Durée | Coût | Qualité (relecteur) |
|---|---|---|---|
| DeepSeek flash, effort bas | 9 min 40 | ≈ 0,32 $ (56k entrée, 3,9M cache, 32k sortie) | 8/10, OK avec détails |
| DeepSeek flash, effort élevé (référence) | ≈ 9 min | comparable | ≈ 9/10, OK avec un détail |

Conclusion provisoire : sur ce type de lot, **l'effort bas ne fait pas gagner de
temps réel** (temps dominé par les outils — installation, tests, typage — et la
lecture du code), coûte à peu près pareil (le cache domine) et perd environ un
point de qualité. **Effort élevé par défaut pour l'implémentation** ; effort bas
seulement quand le raisonnement domine (petites tâches sans suite de tests
lourde). Le levier de vitesse est ailleurs : tests ciblés, sessions courtes,
revue par risque. À confirmer sur d'autres paires (Rust, SQL). Outil de
référence : `~/.local/bin/nexlink-rd`.

# Second rejeu isolé (05:13)

Lot léger (traductions et tests) : DeepSeek flash en effort bas, 14 min, tests
verts, diff de 850 lignes contre 543 pour la référence (plus verbeux) ; la
référence en effort élevé avait pris environ 14 min. Aucun gain de délai :
**l'effort bas est écarté pour l'implémentation** (critère
[0023](../decisions/0023-critere-d-adoption-des-reglages.md)).

# Revue de code par un modèle bon marché (17:40)

Rejeu isolé d'une revue réelle (lot bloqué par le relecteur Codex sur 2 défauts),
avec DeepSeek flash en effort élevé : 6 min 17 s, ≈ 55k tokens en entrée (1,9M en
cache) et 34k en sortie, une fraction de dollar. **Il retrouve les 2 défauts de
référence**, avec la même analyse et des sondes exécutables ; il ajoute 1 faux
positif (dû à une politique du projet qu'il ignorait) et 1 remarque juste non
bloquante. Conclusion provisoire : **rappel égal, précision un peu moindre** sur
cet échantillon. Suite : pilote en **double revue** sur des lots de classe
normale avant tout transfert de responsabilité ; les revues sensibles restent
aux relecteurs Codex et Claude. Implication pour ameesh : la politique du projet
doit être fournie au relecteur (canon), pour éviter les faux positifs de ce type.
