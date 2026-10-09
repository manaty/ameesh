---
type: Study
title: "Étude — sessions parallèles d'une persona (fin de L52)"
description: "Comment une persona peut mener plusieurs sessions à la fois, une par lot, sans casser l'exclusivité des baux ni l'acheminement du courrier : deux voies comparées (bail par session dans une nouvelle table, ou sessions filles portées par des agents éphémères), recommandation, lots et questions."
status: draft
tags: [v2, persona, session, bail, runner, courrier]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-09T12:45:00+02:00" }
stale_after: 2027-01-09
sources:
  - { resource: "../decisions/0025-une-session-par-lot.md", title: "Décision 0025 (une session par lot)" }
  - { resource: "../decisions/0029-persona-et-session.md", title: "Décision 0029 (persona et session)" }
  - { resource: "../decisions/0032-persona-roles-et-sessions.md", title: "Décision 0032 (persona, rôles, sessions)" }
  - { resource: "persona-et-harnais.md", title: "Étude persona et harnais (lot P2)" }
  - { resource: "ameesh-v2.md", title: "Étude ameesh v2" }
---

# Question

La décision 0029 dit qu'une persona peut avoir **plusieurs sessions en
parallèle**, une par lot (0025). La v1 ne le permet pas, et voici pourquoi :

| Élément | Aujourd'hui | Ce qui empêche le parallélisme |
|---|---|---|
| Bail | une colonne de `agent_registry` (`lease_owner`, `lease_epoch`) | un seul exécuteur, donc un seul tour à la fois, par nom |
| Session | `agent_registry.session_id` | une seule session courante |
| Exécuteur | un `AgentWorker` par nom | un fil d'exécution par persona |
| Courrier | adressé à un nom | rien ne dit quelle session doit le lire |
| Compte, budget, pression | jugés par nom et par hôte | à répartir entre sessions |

Le premier pas est fait : `persona_sessions` garde l'historique des sessions
(L52, première partie).

# Deux voies

## A — un bail par session (nouvelle table)

`persona_sessions` gagne les colonnes du bail (`lease_owner`, `lease_epoch`,
`lease_expires_at`, `status`, `current_prompt`). Un exécuteur réclame une
**session**, plus un nom. L'`AgentWorker` devient un travailleur par
(persona, session). Le courrier porte un lot (`work_item_id`) et va à la
session de ce lot, ou à la session principale.

* **Pour** : le modèle cible, propre et définitif.
* **Contre** : la refonte la plus large de la v2. Tous les verrous et
  prédicats de bail sont réécrits (réclamation, renouvellement, reprise
  monotone, `reap`, alertes, `attach`, `adopt`, `resume`), avec le risque de
  régression le plus élevé, sur le cœur qui tourne en production.

## B — des sessions filles portées par des agents éphémères

On réutilise ce qui existe : `ameesh agent spawn` crée déjà des **agents
éphémères** (échéance, créateur, non gouvernés par le canon). Une session
parallèle d'une persona `verif-a` sur le lot 52 devient un agent éphémère
`verif-a~52` :
- il hérite de la persona son responsable, ses capacités, ses outils, son
  harnais et son dépôt de mémoire ;
- il a son propre bail, sa propre session et sa propre ligne de registre.

Le courrier adressé à `verif-a` avec `--lot 52` est remis à `verif-a~52`
s'il existe. La fille écrit dans le journal de mémoire de la persona (L54),
ce qui est sans conflit puisqu'il y a un fichier par session. Elle est
inscrite dans `persona_sessions` sous la persona mère. À la clôture du lot,
elle consolide sa mémoire, puis s'éteint.

* **Pour** :
  - aucun changement du modèle de bail, qui est éprouvé ;
  - chaque session a déjà tout ce qu'il lui faut : bail, budget, pression,
    `attach`, alertes ;
  - le risque est réduit ;
  - le travail est découpable.
* **Contre** :
  - deux notions coexistent (persona et fille), et il faut les montrer comme
    une seule dans `ameesh list` et `activity` ;
  - le nom porte le lot (`~`) ;
  - les budgets doivent être sommés par persona ;
  - un agent éphémère n'est pas gouverné par le canon, donc l'héritage doit
    être recopié à la création et relu à chaque synchronisation.

# Recommandation

**B pour la v2, A plus tard si B montre ses limites.** B livre le parallélisme
sans toucher au cœur qui tourne. Le format visible reste celui de A
(`persona_sessions`, une session par lot), ce qui permet de passer de B à A
sans changer ce que voient les humains.

# Lots

| Lot | Contenu |
|---|---|
| L52b | création d'une fille (`ameesh session open <persona> --lot N`), héritage recopié et relu par `canon sync`, inscription dans `persona_sessions` |
| L52c | acheminement du courrier par lot vers la fille, puis la mère par défaut ; rapatriement à l'extinction |
| L52d | vue unique : `ameesh list`, `activity`, `sessions` regroupent mère et filles ; budget par persona = somme |
| L52e | fin de lot : tour de mémoire (L54) puis extinction de la fille ; alerte si une fille survit à son lot |

# Questions au propriétaire

1. **B (filles éphémères) d'abord** : d'accord ?
2. **Qui ouvre une session parallèle ?** L'orchestrateur, quand il affecte un
   lot à une persona déjà occupée ? Ou ameesh, automatiquement ?
3. **Combien de sessions au plus par persona ?** Je propose 3 par défaut,
   réglable dans la fiche de la persona.
