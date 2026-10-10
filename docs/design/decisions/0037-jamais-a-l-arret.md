---
type: Decision
title: "ameesh ne s'arrête jamais, il s'améliore"
description: "Une file d'amélioration continue, faite de lots à valeur attendue, est prise automatiquement par les agents au repos quand aucun lot ne les attend ; jamais de geste irréversible ou de production, forfaits d'abord, débit borné ; une alerte quand la file est vide."
status: proposed
tags: [exploitation, sous-utilisation, lots, amelioration-continue]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "demande du propriétaire dans sa conversation avec mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-10T21:30:00+02:00" }
sources:
  - { resource: "0036-auditeur-interne.md", title: "Auditeur interne" }
  - { resource: "0034-consommer-d-abord-ce-qui-expire.md", title: "Consommer d'abord ce qui expire" }
  - { resource: "0030-pas-de-travail-sans-reveil-possible.md", title: "Pas de travail sans réveil possible" }
---

# Contexte

L94 fait alerter la sous-utilisation : forfait perdu à la remise à zéro,
agents réveillables au repos pendant que du travail attend. Mais quand rien
n'attend, l'alerte se tait et les agents au forfait dorment : la capacité
payée d'avance est perdue ([0034](0034-consommer-d-abord-ce-qui-expire.md)).
Le propriétaire veut qu'ameesh ne soit « jamais à l'arrêt » : un agent au
repos doit trouver de lui-même quelque chose d'utile à faire, sans qu'un
humain ait à le lui confier, et sans que cela devienne du travail pour occuper
ni un risque pour la production.

# Décision

1. **Une file d'amélioration continue.** Un élément est un lot
   (`work_items`) de type `improvement`, en `intake`, sans assigné. Il porte
   une **valeur attendue** en une phrase et un **score** de 1 à 100, tous deux
   obligatoires (refusés par la CLI et par la base) : pas de valeur, pas
   d'élément. S'y ajoutent une priorité (1 haute, 2 normale, 3 basse), la
   source (constat de l'auditeur [0036](0036-auditeur-interne.md), test
   instable, dette, alerte récurrente…), une équipe et des capacités exigées.
   Pas de table neuve : cycle, journal, jalons, vue d'avancement et alertes des
   lots s'appliquent tels quels. `ameesh work backlog add|list`.
2. **Prise automatique** à chaque passage d'`ameesh notify` : un agent
   réveillable au repos depuis `--take-idle` (30 min) prend l'élément le mieux
   classé (priorité, puis valeur, puis ancienneté) qui correspond à son équipe
   et à ses capacités. Le lot lui est assigné par l'attribution gardée
   ([0030](0030-pas-de-travail-sans-reveil-possible.md)) et annoncé par un
   courrier lié au lot, qui le réveille.
3. **Seulement si aucun lot ne l'attend.** L'agent n'a ni lot ouvert, ni
   courrier non lu, ni consigne en attente, ni lot de session encore ouvert. Et
   tant qu'un lot du projet attend un preneur (ouvert, sans assigné, hors
   file), personne ne prend d'amélioration : la demande d'un humain passe
   avant, c'est à l'orchestrateur ou à l'humain de la confier (`idle_capacity`
   le signale).
4. **Jamais un geste irréversible ou de production.** Le courrier de prise
   l'interdit en toutes lettres (déploiement, fusion, suppression de données,
   serveur de production, dépense engagée) : l'agent le propose à un humain
   et s'arrête là. Le circuit du projet (relecture, intégration par qui en a
   le droit) reste le seul chemin vers la production. Un élément qui a perdu
   sa valeur est rendu (`ameesh work close --abandoned`), pas travaillé pour
   occuper.
5. **Ce qui coûte, borné.** Les agents au forfait d'abord ; un agent payé au
   token ne prend rien sauf `--take-paid`, et jamais pendant `balance_low`.
   Pas de prise si un plafond de budget est atteint, si un agent du mesh est
   en pause budget, ni sur un hôte sous pression, sur batterie faible ou non
   prêt. Au plus `--take-max-per-hour` prises (2) par heure glissante, comptées
   en base pour tout le mesh. L'attribution est conditionnelle : deux preneurs
   concurrents ne prennent pas le même élément.
6. **File vide, alerte.** `backlog_empty` est levée quand des agents
   réveillables sont au repos et que la file n'a plus rien à prendre (et
   qu'aucun lot du projet n'attend) ; `ameesh notify` la pousse à l'humain
   responsable, invité à remplir la file.

# Conséquences

- Un élément en file n'est ni un « lot sans assigné » d'`idle_capacity`, ni un
  lot stagnant : il attend son preneur.
- Le cœur reste générique : rien ne nomme un métier ; la source d'un élément
  est un texte libre.
- L'auditeur interne (0036) est la source naturelle de la file : ses constats
  qui ne relèvent pas d'un geste réversible deviennent des éléments à valeur
  attendue.
- `--take-idle 0` (ou `AMEESH_TAKE_IDLE=0`) coupe la prise ; `--dry-run`
  montre les prises qui seraient faites sans rien assigner.
