---
type: Decision
title: "Une session neuve par lot"
description: "Un agent de construction ou de relecture ouvre une session neuve pour chaque lot et la garde jusqu'à la fusion ; les orchestrateurs gardent une session longue, tournée avec résumé."
status: stable
tags: [sessions, vitesse, qualite, cout]
decided_by: human:smichea
decision_date: 2026-10-05
attestation: "décision du propriétaire dans sa conversation avec mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-05T04:20:00+02:00" }
---

# Contexte

Un agent qui a enchaîné six lots sur la même session (contexte compacté, plus
de 60 millions de tokens relus par tour) a raté des consignes et présenté un
rebase comme un correctif, trois gels de suite, alors qu'il tournait déjà en
effort maximal. Voir le constat n° 5 des
[constats sur les modèles](../etudes/constats-modeles-2026-10.md).

# Décision

1. **Agents de construction et de relecture : une session neuve par lot**,
   ouverte avec un brief qui se suffit à lui-même (état, périmètre, règles,
   défauts connus). La session est **gardée jusqu'à la fusion du lot**,
   relectures et corrections comprises, puis abandonnée.
2. **Relecteurs** : une session neuve par lot relu, gardée pour les re-gels du
   même lot.
3. **Orchestrateurs** : session longue, avec rotation et résumé de reprise
   quand elle grossit, et un état écrit (mémoire, journal) qui survit à la
   perte de la session.
4. **Surveillance** : la taille du contexte relu par tour est suivie ; un seuil
   déclenche une alerte, et une session qui rate des consignes est tournée sans
   attendre.

# Conséquences

- ameesh v1 : en plus de la rotation sur la taille (L11), **rotation au
  changement de lot** (le `work_item` du tour change → nouvelle session avec le
  résumé du lot), réglable par agent : `par-lot` (défaut pour construction et
  relecture), `taille`, `jamais` ; alerte « session trop grosse ». Lot L26.
- En attendant, la règle s'applique à la main avec la boucle v0 (session neuve
  au démarrage d'un lot).
