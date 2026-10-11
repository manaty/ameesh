---
type: Decision
title: "Un auditeur interne vérifie chaque heure l'exploitation d'ameesh"
description: "Une persona auditeur, sur le modèle le plus capable que l'organisation lui attribue et au niveau d'effort le plus élevé de son harnais (amendement du 2026-10-11), passe chaque heure et à chaque alerte urgente sur l'état du mesh, et mène chaque nuit et après chaque incident notable une analyse profonde des pertes de temps jusqu'à leur cause racine. Elle corrige, fusionne et déploie seule les bugs d'ameesh quand la CI est verte, agit seule sur le réversible et propose le reste ; chaque règle acceptée devient une décision."
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
   *Amendé le 2026-10-11, voir ci-dessous : le modèle n'est plus fixé par la
   décision ; c'est le plus capable que l'organisation attribue à l'auditeur,
   au niveau d'effort le plus élevé de son harnais.*
2. **Rythme** : un passage par heure, et un passage à chaque alerte urgente.
   Le passage est court : la liste de contrôle, les gestes permis, le rapport.
   *Amendé le 2026-10-11 : s'y ajoute une analyse profonde chaque nuit et
   après chaque incident notable.*
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
   *Amendé le 2026-10-11 : il corrige, fusionne et déploie seul les bugs
   d'ameesh, CI verte, et ne suspend jamais le travail des autres.*
5. **Compte rendu** : un rapport horaire court dans le fil du projet ameesh ;
   une alerte poussée seulement quand il faut une décision humaine ; un
   résumé quotidien. *Amendé le 2026-10-11 : un rapport à la fin de chaque
   analyse profonde, un lot par cause racine, et la mesure de son utilité.*
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
  *Amendé le 2026-10-11 : il touche au code d'ameesh, par PR et CI, pour les
  seules corrections de bugs.*

# Amendement du 2026-10-11 : analyse profonde en effort maximal, et correction des bugs

Demande du propriétaire le 2026-10-11 ; consigne [AUDITEUR.md](../../AUDITEUR.md)
mise à jour.

## Constat

L'auditeur n'a jamais tourné : sa fiche de canon n'a pas été fusionnée.
Entre-temps, dans la nuit du 2026-10-10, une analyse menée sans lui a montré
ce que le propriétaire attend. Pour chaque perte de temps d'un projet servi par
le mesh, elle a suivi la chaîne des causes jusqu'à sa racine dans ameesh, puis
elle a lancé les correctifs. Quatre angles, menés en parallèle par des
sous-agents en lecture seule : la chronologie d'un chemin critique et de ses
attentes ; le coût de la coordination ; les causes dans le code d'ameesh ; les
incidents d'exécution. Elle a trouvé une dizaine de causes racines ; quatre
correctifs sont partis en PR et des lots ont été ouverts.

Le 2026-10-11, le propriétaire a précisé ce qu'il attend : « un agent qui
régulièrement analysait l'activité d'ameesh et corrigeait les problèmes,
exactement comme ce que nous sommes en train de faire ; il est important que
cet agent soit en effort max ». Il a fixé le même jour que **les corrections de
bugs se fusionnent et se déploient sans sa validation**. Les nouvelles
fonctions, les migrations qui changent le comportement, les gestes en
production des projets, les dépenses et les secrets restent ses décisions.

Le passage horaire de §2 ne suffit pas à cela : avec une dizaine de commandes,
il voit les symptômes (les alertes), pas la chaîne qui y mène.

## Modèle et effort (§1)

**Le modèle n'est plus fixé par la décision.** L'auditeur tourne sur le modèle
le plus capable que l'organisation lui attribue, au niveau d'effort le plus
élevé de son harnais. Le choix concret (harnais, fournisseur, modèle, effort,
authentification par abonnement ou par clé) relève de sa fiche de canon ; le
compte est choisi à chaque session par ameesh (amendement de
[0034](0034-consommer-d-abord-ce-qui-expire.md)). Les capacités restent
`[read, report-drift, propose]` : fusionner une correction de bug n'est pas
`approve` (R8), c'est la règle du propriétaire.

L'effort se règle par agent, `ameesh set auditeur effort=<niveau>`. ameesh
passe la valeur telle quelle au harnais, par son descripteur (option de ligne
de commande ou fichier de configuration), sans la valider : le niveau retenu
est le plus élevé que le harnais accepte, tel que son aide en ligne de commande
le liste. ameesh ne lit pas encore d'effort dans le canon (seul `model` est
synchronisé) : la mise en service applique le réglage, et un lot rendra
l'effort déclaratif au canon.

Le coût suit [0019](0019-budgets-et-routage.md) : rythme des forfaits,
plafonds de dépense et pause quand tous les comptes sont au seuil s'appliquent
tels quels. Une analyse profonde en effort maximal, avec ses sous-agents, coûte
bien plus qu'un passage court ; la capacité payée d'avance qui se perdrait à la
remise à zéro (`plan_underused`) est son meilleur emploi.

## Deux sortes de passages (§2)

1. **Passage court**, chaque heure et à chaque alerte urgente : la liste de
   contrôle de §3, gardée, les gestes permis, le rapport. Un incident notable
   n'y est pas creusé : il ouvre une analyse profonde.
2. **Analyse profonde**, chaque nuit, après chaque incident notable et à la
   demande du propriétaire. Elle reprend la méthode de la nuit du 2026-10-10 :
   quatre angles menés en parallèle, chacun par un sous-agent en lecture
   seule :
   1. *chronologie* : le chemin critique de chaque projet actif et ses attentes
      (qui attendait quoi, de qui, combien de temps) ;
   2. *coût de la coordination* : messages, tours, copies, accusés de
      réception, diffusions, relais, rotations ;
   3. *causes dans le code d'ameesh* : pour chaque perte, le fichier et la
      ligne, le mécanisme, le correctif, le test qui la reproduit ;
   4. *incidents d'exécution* : journaux des exécuteurs, pression des hôtes,
      tours tués, ménage, CI.

   L'auditeur fait lui-même la synthèse. Il relie chaque perte de temps à sa
   cause racine, avec une preuve à chaque maillon, classe les causes par temps
   perdu et écarte celles qu'un lot ou une PR couvre déjà.

Est un incident notable : un agent arrêté après des échecs, un tour tué ou clos
par un plafond, une pression critique sur un hôte, une ressource orpheline, une
CI rouge sur la branche principale, un retour arrière de déploiement, une
attente de plus d'une heure sur un chemin critique, une alerte urgente qu'un
passage court n'a pas résolue.

Une seule analyse à la fois, et au plus deux analyses après incident par jour en
plus de celle de la nuit : au-delà, l'incident attend la nuit. Sous pression de
l'hôte, les angles passent l'un après l'autre ; sous pression critique,
l'analyse attend. Les sous-agents tournent dans son harnais, sous son compte :
l'analyse n'engage aucune dépense.

## Ce qu'il produit (§5)

- **Un rapport court au propriétaire**, dans le fil d'ameesh : à chaque passage
  court, comme avant, et à la fin de chaque analyse profonde (temps perdu
  trouvé, causes racines, ce qui est corrigé, ce qui est proposé).
- **Un lot par cause racine**, avec sa chaîne de causes, ses preuves et la
  mesure du temps perdu : un lot `bug` qu'il corrige lui-même, un élément de la
  file d'amélioration ([0037](0037-jamais-a-l-arret.md)), ou une proposition de
  règle. Une cause hors d'ameesh (règle d'un projet, configuration d'un hôte,
  limite d'un fournisseur) va à qui en a la charge.
- **Des correctifs en PR pour les bugs d'ameesh.** Il les fusionne et les
  déploie quand la CI est verte ; il propose tout le reste.

## Marge d'action (§4)

Les deux niveaux restent, et un troisième s'insère entre eux.

1. *Il agit seul, parce que c'est réversible* : inchangé, sauf ce qui
   suspendrait le travail d'un autre. `ameesh restart` ne vise qu'un agent
   hors tour ; une baisse de plafond ne doit mettre personne en pause sur le
   moment, sinon il alerte. Le démarrage de la réserve de CI reste permis :
   c'est une dépense que cette décision permettait déjà, bornée par l'arrêt de
   la réserve après 30 minutes d'inactivité.
2. *Il corrige les bugs d'ameesh, les fusionne et les déploie.* Un bug est un
   comportement contraire à une décision, à la spécification, à la
   documentation ou à l'intention manifeste du code ; sa correction le rétablit
   sans ajouter de commande, d'option, de clé de configuration, d'alerte ni de
   règle. En cas de doute, c'est une évolution, et il la propose. Le circuit :
   - un lot `bug` et une branche dans un worktree neuf ;
   - un test qui échoue avant le correctif et passe après (sauf pour une
     correction de documentation), puis le plus petit correctif ;
   - une PR qui dit la perte constatée, la chaîne des causes et ses preuves, le
     correctif et son inverse ;
   - la CI verte sur une base à jour ;
   - avant la fusion, le verdict d'un relecteur autre que lui : un sous-agent
     neuf, en lecture seule, qui ne connaît que le lot, les preuves et le
     diff ; pour la classe sensible de [0018](0018-vitesse-des-agents.md) (SQL
     et migrations, sécurité, autorisation, exécuteur, porte, approbation,
     lecture du canon, déploiement), un autre agent du mesh, d'un autre
     fournisseur quand il y en a un ;
   - la fusion, puis la CI de la branche principale verte sur le commit
     fusionné ;
   - le déploiement par `deploy/mise-a-jour` : vérifier, sauvegarder,
     installer, redémarrer chaque exécuteur hors tour, contrôler ; `retour` au
     premier contrôle en échec, puis une PR qui annule le correctif. Il lance
     le redémarrage hors de son propre tour, et son exécuteur redémarre comme
     les autres, entre deux tours. Une correction qui porte une migration n'est
     déployée par lui que si la migration est additive et ne change aucun
     comportement, de sorte que `retour` reste possible sans toucher à la base.
     Déployer un commit déploie tout ce qui le sépare du commit installé : si
     cet écart porte autre chose que des corrections de bugs (une évolution
     fusionnée mais pas encore déployée, une migration qui n'est pas additive),
     il ne déploie pas, et sa correction attend la décision du propriétaire.
     S'il n'a pas accès à l'un des hôtes du mesh, il ne déploie pas à moitié :
     la correction reste fusionnée et il le signale.
3. *Il propose, l'humain décide* : inchangé. S'y ajoutent les évolutions
   (nouvelles fonctions), les migrations qui changent le comportement, la
   publication d'une version, et toute correction qu'il ne peut pas mener
   jusqu'au bout du circuit ci-dessus.

## Garde-fous

- Jamais de geste en production d'un projet servi par le mesh, jamais de
  dépense nouvelle, jamais de secret lu, copié ou écrit.
- Il ne modifie jamais sa consigne, cette décision, sa fiche de canon, ses
  capacités ni aucune permission (rôles de la base, droits sur les dépôts,
  protection des branches, accès aux hôtes). S'il pense qu'il le faudrait, il le
  dit dans son rapport.
- Ses changements d'ameesh passent toujours par une PR et la CI, jamais par une
  écriture directe sur la branche principale. Il ne rend pas la CI verte en la
  changeant : une PR qui touche la CI ou les scripts de test, ou qui retire,
  saute ou affaiblit un test, n'est jamais fusionnée par lui.
- Chaque déploiement suit `deploy/mise-a-jour` (vérifier, sauvegarder, retour
  possible) et redémarre les exécuteurs hors tour.
- Il ne suspend jamais le travail des autres de son propre chef : ni arrêt, ni
  pause, ni interruption, ni changement de mode, ni redémarrage d'un agent en
  tour, ni processus ou conteneur tué, même orphelin. Quand il le faudrait, il
  alerte le responsable.
- Ses sous-agents ne font que lire : aucun courrier, aucun geste d'ameesh,
  aucune écriture git ou GitHub ; la base se lit sous un rôle en lecture seule.
- Jamais `approve` (R8).

## Mesure de son utilité

Chaque semaine, dans le fil d'ameesh :

- **le temps perdu évité** : chaque lot de cause porte la perte mesurée et la
  requête qui la mesure. Sept jours après le déploiement du correctif,
  l'auditeur refait la même mesure sur une période de même durée ; la
  différence est le temps évité. Une perte qui ne baisse pas rouvre la cause ;
- **les causes fermées** : correctif déployé ou règle acceptée ; les récidives
  sont comptées à part ;
- **les faux positifs** : constat démenti (lot abandonné avec la note « faux
  positif »), PR fermée faute de défaut réel, correctif annulé ; rapportés au
  nombre de constats ;
- **son coût** : ses tours, sous-agents compris, en part de forfait ou en
  dépense.

Ces chiffres sont le critère du propriétaire pour garder, réduire ou étendre
l'auditeur ([0023](0023-critere-d-adoption-des-reglages.md)).

## Inchangé

La liste de contrôle (§3) ; l'apprentissage (§6) : une règle acceptée devient
une décision ; l'alerte poussée seulement quand il faut une décision humaine ;
le résumé quotidien ; les capacités au canon ; jamais `approve`.

## Conséquences de l'amendement

- La fiche de canon, dont la PR n'est pas fusionnée, prend le harnais, le
  fournisseur, le modèle et le mode d'authentification choisis par
  l'organisation, et déclare l'effort. La mise en service applique
  `ameesh set auditeur effort=<niveau>`.
- Le propriétaire crée un rôle de connexion en lecture seule sur la base,
  membre des rôles du contrat de supervision
  (`deploy/sql/role-superviseur.sql` et `role-superviseur-contenus.sql`) :
  c'est une permission. Sans ce rôle, l'analyse se contente des commandes
  `ameesh … --json`.
- Un troisième minuteur envoie chaque nuit le courrier « Analyse profonde ».
- **Bug à corriger avant la première analyse profonde.** Claude Code déclenche
  aussi les hooks pour les outils de ses sous-agents, et l'entrée du hook porte
  alors `agent_id` ; `agent-mail hook` ne le regarde pas. Un message remis à
  `PostToolUse` pendant qu'un sous-agent travaille va donc dans le contexte de
  ce sous-agent et il est marqué livré : le fil principal ne le voit pas. Le
  défaut touche tout agent qui lance des sous-agents. Le lot le vérifiera par
  un test avant de corriger ; en attendant, la consigne demande aux sous-agents
  de recopier tout message reçu, et à l'auditeur de relire le courrier remis
  pendant l'analyse.
- Lots à ouvrir : l'effort déclaratif au canon ; des scripts
  `deploy/mise-a-jour` génériques (ils portent encore les noms et les
  contrôles de la mise à jour L60–L74) ; le routage des alertes urgentes vers un
  agent dans `ameesh notify`, déjà prévu.
- La boucle de [0022](0022-auto-reparation.md) s'applique à ameesh lui-même :
  l'auditeur porte les signaux, le triage, la réparation, la vérification, la
  livraison et la surveillance après livraison. Pour les seules corrections de
  bugs d'ameesh, dans son propre mesh, la livraison va jusqu'au déploiement sans
  humain ; la production des projets reste humaine.
