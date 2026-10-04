---
type: Decision
title: "Vitesse : revue proportionnée au risque, interruption, rotation des sessions, délais mesurés"
description: "ameesh encode les règles de vitesse validées pour Nexlink : politique de revue par classe de risque au canon, interruption de tour de premier ordre, rotation de session avec résumé, sessions robustes au déplacement, métriques de délai par lot."
status: stable
tags: [vitesse, revue, interruption, sessions, metriques]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "décision du propriétaire transmise par l'orchestrateur Nexlink (message de 00:09), rapportée par mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:30:00+02:00" }
sources:
  - { resource: "nexlink:docs/board/messages/2026-10-04-0215-orchestrateur-velocity-rules.md", title: "Règles de vitesse appliquées à Nexlink" }
---

# Contexte

Une retouche d'une ligne coûtait 30 à 45 minutes. Causes observées : même
circuit de revue pour une ligne de style que pour une migration ; messages lus
seulement entre deux tours (un contre-ordre arrive trop tard) ; revérification
complète à chaque fois ; orchestrateur relais obligatoire ; sessions qui
gonflent jusqu'à saturer ; reprise cassée après le renommage d'un dossier.

# Décision

À encoder dans ameesh comme exigences :

1. **Politique de revue par classe de risque**, déclarative, dans le canon OKF,
   par portée de fichiers : **léger** (fusion dès que les tests ciblés sont
   verts, relecture après coup), **normal** (une revue d'un autre éditeur, les
   détails ne bloquent pas), **sensible** (gel, revue avant fusion, accord
   explicite : SQL, sécurité, natif, contrats, production). En cas de doute, la
   classe supérieure.
2. **Interruption de tour de premier ordre** : un message prioritaire préempte
   le tour en cours (arrêt du tour, reprise de la même session avec ce message
   en tête) au lieu d'attendre la fin du tour.
3. **Rotation de session** pilotée par la taille du contexte et la latence des
   tours, avec un **résumé de reprise** produit avant la rotation.
4. **Sessions robustes au déplacement** d'un worktree (renommage de dossier).
5. **Métriques de délai par lot** (demande → gel → revue → fusion), exposées
   par `ameesh list` / `ameesh work`.

# Conséquences

- Exigence R19 ; lot L10 (politique de revue au canon et métriques de délai :
  hors exécuteur) et lot L11 (interruption, rotation, déplacement : dans
  l'exécuteur, après L8).
- La classe de risque d'un changement est calculée depuis les fichiers touchés
  et la politique du canon ; elle alimente aussi la porte (une fusion « légère »
  peut être une action `reversible`).
