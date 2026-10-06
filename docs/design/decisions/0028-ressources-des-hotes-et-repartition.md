---
type: Decision
title: "ameesh surveille les ressources de chaque hôte et répartit les agents en conséquence"
description: "Chaque exécuteur mesure mémoire, swap, CPU et disque de son hôte ; sous pression il retient les nouveaux tours, signale les ressources orphelines et, quand le canon autorise plusieurs hôtes pour un agent, déplace l'agent vers un hôte moins chargé entre deux tours."
status: stable
tags: [placement, ressources, exploitation, multi-hotes]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "demande du propriétaire dans sa conversation avec mesh-design, après une saturation mémoire du poste (12 Go de swap) ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T17:30:00+02:00" }
---

# Contexte

Le placement v1 est déclaratif (un agent → un hôte, `max_agents` par hôte).
Rien ne mesure la charge réelle : un poste a saturé sa mémoire (navigateur,
suites de tests des agents, une douzaine de bases de test oubliées depuis 17 h)
sans qu'aucun outil ne le voie ni n'agisse.

# Décision

1. **Mesure** : chaque exécuteur relève périodiquement les ressources de son
   hôte (mémoire disponible, swap, charge CPU, disque libre, nombre de tours en
   cours) et les publie en base ; `ameesh hosts [--json]` les montre, avec
   l'historique court.
2. **Seuils au canon** : la politique d'un `Host` porte des seuils (mémoire
   disponible minimale, swap maximal, charge maximale, disque minimal), avec des
   valeurs par défaut prudentes.
3. **Contre-pression locale** : au-dessus d'un seuil, l'exécuteur ne démarre
   plus de nouveau tour (les tours en cours finissent) et lève une alerte ; en
   cas critique, il met en pause les agents les moins prioritaires, jamais au
   milieu d'un tour.
4. **Ressources orphelines** : l'exécuteur rattache à chaque tour les processus
   et conteneurs qu'il lance (groupe de processus, étiquettes de conteneur) et
   signale ceux qui survivent à leur tour ou à leur agent ; il ne supprime rien
   sans action explicite.
5. **Répartition** : un `Placement` peut désigner plusieurs hôtes admis pour un
   agent (ordre de préférence) ; entre deux tours, un agent passe à un hôte
   admis moins chargé (bail rendu puis repris ; même session si le stockage des
   sessions le permet, sinon rotation avec résumé, [0025](0025-une-session-par-lot.md)).
   La politique d'hôte et les identifiants (mode, comptes, [0027](0027-bascule-automatique-entre-comptes.md))
   restent vérifiés sur l'hôte d'arrivée.

# Conséquences

- Lot L31 ; s'appuie sur L3 (placement), L26 (alertes), L30 (comptes) et L25
  (profil cluster, où chaque pod est un hôte).
- Les seuils et les hôtes admis sont déclaratifs et revus (canon) ; les mesures
  sont de l'état d'exécution (base).
