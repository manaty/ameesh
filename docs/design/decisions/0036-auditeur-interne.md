---
type: Decision
title: "Un auditeur interne vérifie chaque heure l'exploitation d'ameesh"
description: "Une persona auditeur (DeepSeek flash) passe chaque heure et à chaque alerte urgente sur l'état du mesh : agents, forfaits, dépense, machines, CI, lots, sécurité. Elle agit seule sur le réversible et propose le reste ; chaque règle acceptée devient une décision."
status: stable
tags: [exploitation, audit, ressources, budgets, auto-reparation]
decided_by: human:smichea
decision_date: 2026-10-10
attestation: "décision du propriétaire dans sa conversation avec mesh-design ; non signée"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-10T18:30:00+02:00" }
sources:
  - { resource: "../../AUDITEUR.md", title: "Consigne de l'auditeur interne" }
---

# Contexte

Le 2026-10-10, le propriétaire a demandé « qu'ameesh ait un auditeur interne
qui régulièrement, par exemple une fois par heure, regarde que tout se passe
bien, que l'utilisation des ressources est optimale, et adapte les règles si
besoin ».

ameesh lève déjà des alertes : vivacité ([0030](0030-pas-de-travail-sans-reveil-possible.md)),
sous-utilisation (L94), pression des hôtes ([0028](0028-ressources-des-hotes-et-repartition.md)),
solde bas. Il les pousse à l'humain responsable. Personne, en revanche, ne les
relit ensemble, ne fait le geste réversible qui suffit souvent (relancer un
agent, baisser un plafond, arrêter une machine de CI au repos), ni ne
transforme un constat qui se répète en règle.

# Décision

1. **Une persona `auditeur`**, équipe ameesh, responsable `human:smichea`,
   harnais DeepSeek, modèle `deepseek-flash`, clé d'API. Sa fiche est dans le
   canon, avec les capacités `[read, report-drift, propose]` et aucune autre.
2. **Rythme** : un passage par heure, et un passage à chaque alerte urgente.
   Le passage est court : la liste de contrôle, les gestes permis, le rapport.
3. **Liste de contrôle**, tenue dans [la consigne](../../AUDITEUR.md) avec
   les commandes exactes :
   - agents bloqués, arrêtés ou tournant à vide ; tours longs ; sessions qui
     grossissent ;
   - forfaits sous-employés ou au seuil ; dépense réelle face à l'estimation ;
     solde du fournisseur payé au token ;
   - charge du poste et des serveurs ;
   - file des runners de la CI Nexlink et état de la machine de réserve, lus
     par l'API GitHub, sans secret dans la consigne ;
   - lots sans agent et engagements en retard ;
   - dérives de sécurité visibles depuis ameesh.
4. **Marge d'action en deux niveaux**, écrite dans la fiche :
   1. *Il agit seul, parce que c'est réversible* : `ameesh resume` ou
      `ameesh restart` d'un agent bloqué ou d'une session trop grosse ; relance
      de l'orchestrateur par courrier ; **baisse** d'un plafond
      (`ameesh budget set`, jamais vers le haut) ; démarrage ou arrêt de la
      réserve de CI par le script existant si son accès le permet, sinon il le
      signale.
   2. *Il propose et l'humain décide* : tout changement de règle (choix des
      comptes, seuils, plafonds à la hausse), l'ajout ou le retrait de
      machines, toute dépense nouvelle. Il passe par une PR sur le canon ou une
      demande d'approbation.

   Il n'utilise jamais `approve` (R8, [0012](0012-autorite-par-ameesh-approve.md)).
5. **Compte rendu** : un rapport horaire court dans le fil du projet ameesh ;
   une alerte poussée seulement quand il faut une décision humaine ; un
   résumé quotidien.
6. **Apprentissage** : chaque règle proposée puis acceptée devient une
   décision écrite. C'est la boucle d'auto-réparation de
   [0022](0022-auto-reparation.md), appliquée à l'exploitation.

# Conséquences

- Fiche `agents/auditeur.md` et placement sur le poste dans le canon manaty ;
  mise en service par l'exécuteur (`ameesh-runner-agent@auditeur`), après la
  fusion de la fiche.
- **Rythme en v1** : l'exécuteur ne relance un agent au repos qu'une fois
  (`--idle-nudge`), puis attend du courrier. Le passage horaire vient donc
  d'un minuteur systemd qui lui écrit, et le passage sur alerte urgente d'un
  second minuteur qui relit `ameesh alerts`. Les deux sont décrits dans la
  consigne. Un lot à ouvrir rendra les alertes routables vers un agent dans
  `ameesh notify`, ce qui remplacera le second minuteur.
- L'auditeur ne touche ni au code, ni au canon `main`, ni aux secrets ; ses
  gestes du niveau 1 sont tous annulables par une commande inverse.
