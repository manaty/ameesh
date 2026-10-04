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
