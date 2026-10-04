---
type: Decision
title: "Un service ameesh liste, découvre et évalue les modèles pour guider le routage"
description: "Les modèles changent sans cesse : ameesh tient un catalogue des modèles disponibles, découvre les nouveaux, les évalue sur des tâches de référence, et en tire des recommandations de modèle et d'effort par classe de tâche."
status: stable
tags: [modeles, catalogue, evaluation, routage, budget]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "rapportée par mesh-design depuis sa conversation avec le propriétaire ; non signée (R13 pas encore en service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:45:00+02:00" }
---

# Contexte

Le routage modèle/effort par tâche ([0019](0019-budgets-et-routage.md)) suppose
de savoir quels modèles existent, ce qu'ils coûtent et ce qu'ils valent pour
chaque type de tâche. De nouveaux modèles sortent sans cesse.

# Décision

Un **service ameesh** :

1. **liste** les modèles utilisables, par fournisseur et par harnais ;
2. **découvre** les nouveaux (et ceux qui disparaissent ou changent de prix) ;
3. **évalue** les modèles et les niveaux d'effort sur des tâches de référence ;

pour que **les orchestrateurs puissent déterminer quel modèle et quel effort
utiliser pour quelle tâche**.

# Conséquences

- **Découverte** (sans coût en tokens) : listes de modèles des API des
  fournisseurs et options des harnais (version, modèles et efforts acceptés),
  barèmes ; un changement (nouveau modèle, retrait, prix) produit un événement
  dans le fil de l'équipe concernée. Les identifiants d'API restent des secrets
  de l'hôte ([0014](0014-orchestrateurs-a-tours-et-placement.md)).
- **Catalogue** : état d'exécution dans la base (modèles vus, première et
  dernière apparition, prix, fenêtres de contexte, efforts) ; les **rapports
  d'évaluation** sont publiés dans le canon (concepts OKF, au besoin
  `Attested Computation` pour les métriques), donc lisibles et revus.
- **Évaluation** : suites de **tâches de référence par classe** (correctif
  léger, implémentation normale, revue de sécurité avec défauts connus à
  trouver, rédaction, traduction…), idéalement tirées de lots réels dont
  l'issue est connue ; mesures : réussite (tests verts, défauts trouvés),
  coût, latence, tokens, par niveau d'effort. Une campagne d'évaluation
  **consomme du budget** : c'est une action **coûteuse**, soumise à la porte
  (reçu ou approbation permanente bornée) et aux garde-fous de forfait (0019).
- **Recommandation** : `ameesh models recommend <classe de tâche>` propose
  modèle et effort ; la **politique de routage** reste dans le canon et ne
  change que par proposition revue (l'orchestrateur propose, l'humain
  responsable valide).
- Exigence R21 ; lots L14 (catalogue et découverte) et L15 (évaluation et
  recommandation).
