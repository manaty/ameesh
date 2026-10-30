# Débit du mesh : file remplie, mesure de l'auditeur, paquets en parallèle (manaty/ameesh#3256)

Spécification co-écrite avec l'owner le 2026-10-28 (session du bureau). Les
décisions marquées **[owner]** sont les siennes, le reste en découle.

Lots : **A** (remplir la file), **B** (mesure de débit de l'auditeur), **C**
(second paquet en parallèle). Le paquet plus rapide est #4271 et ne fait pas
partie de cette spec.

## 0. Le problème

Depuis deux semaines, chaque « on avance ? » de l'owner trouve la plupart des
agents à l'arrêt, à chaque fois pour une cause différente : staging, quotas,
holds, paquet. À chaque fois, on ouvre un lot qui traite cette cause-là. La
boucle elle-même n'est pas traitée.

Le 28/10 à 14:52Z :

- 32 agents sur une quarantaine étaient au repos. `idle_capacity` relevait 23
  agents réveillables et **0 lot démarrable**, alors que `ameesh debit`
  donnait `idle_agents_with_work = []`.
- 78 % de la fenêtre de 5 h du compte Claude primaire allaient être perdus au
  reset. Codex avait consommé 4 % de sa semaine.
- 18 lots ameesh relus attendaient le paquet h-07, qui testait ses unités une
  par une, à raison de 20 à 30 min chacune.
- Les deux pilotes enchaînaient des tours de moins de 2 min (`turn_churn` :
  17 sur 18 pour mesh-design, 12 sur 13 pour l'orchestrateur).

Cause : aucun rôle n'a pour tâche de remplir la file.

- L'auditeur mesure les attentes des lots existants (`debit --check`,
  `idle_agents_with_work`). Pour lui, une file vide veut dire « rien à
  signaler ».
- Ses droits se limitent à lire, signaler et proposer, et sa consigne renvoie
  les alertes de capacité au pilote.
- `idle_capacity` et `plan_underused` désignent l'owner comme responsable. Les
  pilotes les reçoivent, ne trouvent aucun lot démarrable et terminent leur
  tour.

## 1. Vocabulaire

- **Agent réveillable** : un agent d'exécution qui n'est ni arrêté, ni retiré,
  ni en pause budget. Les relecteurs désignés, les pilotes, l'auditeur et
  mesh-design n'en font pas partie : ce sont des agents critiques ou des
  relecteurs (§11 du canon).
- **Lot démarrable** : la définition de `split_startable`
  (`sous_utilisation.py`). C'est un lot ouvert, sans prérequis ouvert, qui
  n'attend ni humain ni exécutant externe et qu'un agent réveillable du projet
  peut prendre.
- **Au repos** : sans tour actif ni bail depuis le dernier tour.
- **Heures-agent au repos** : somme, sur la fenêtre, du temps passé au repos
  par les agents réveillables pendant que le projet avait du travail
  démarrable, ou qu'il avait une file vide alors que sa feuille de route
  n'était pas épuisée.
- **Feuille de route épuisée** : le pilote déclare, sur le lot de découpage,
  qu'il ne reste rien à découper. Il le fait avec une note
  `roadmap: épuisée <projet>` sur ce lot, sourcée (fichier de feuille de route,
  décision owner).

## 2. Lot A : remplir la file

### 2.1 Déclencheur **[owner]**

Le déclencheur porte sur un projet. Il se déclenche quand **au moins 20 % de
ses agents réveillables sont au repos depuis 30 min** et que le projet
**n'a aucun lot démarrable**. Le calcul est déterministe, dans le code (§7 du
canon), et ses deux paramètres viennent du bloc mécanique du canon :
`file.repos_part = 0.20` et `file.repos_s = 1800`.

### 2.2 Geste **[owner]**

1. ameesh crée un lot de type `evolution`, titré « Découper la feuille de
   route de <projet> : des lots démarrables pour N agents ». Il est assigné au
   pilote du projet, en priorité 1.
   - N vaut le nombre d'agents réveillables au repos.
   - Le corps du lot cite la mesure (agents au repos, depuis quand, quota perdu
     au reset) et la source de la feuille de route du projet.
   - Un seul lot de découpage est ouvert par projet : tant qu'il l'est, le
     déclencheur ne crée rien de plus.
2. Le pilote crée des lots démarrables, d'au plus une journée chacun et
   conformes à la classe du §1 du canon. Le lot de découpage se ferme dès que
   la file compte des lots démarrables pour N agents.
3. **Repli** : si, une heure après sa création, le lot de découpage n'a donné
   lieu à aucun lot démarrable, l'auditeur crée lui-même les lots au niveau 1.
   - Créer un lot est réversible : il suffit de le fermer.
   - Il ne touche ni au code ni à la priorité des lots du pilote.
4. Le pilote n'attend pas d'être réveillé par l'alerte : le lot entre dans son
   brief de reprise comme un lot à priorité 1.

### 2.3 Feuille de route épuisée **[owner]**

Quand le pilote note la feuille de route épuisée :

- les agents d'exécution réveillables en trop du projet sont mis en sommeil :
  ils restent déclarés et réveillables, mais sans tour ni jeton ;
- les hôtes burst sont rendus par le drainage de #3167 (partie B, #4148) ;
- l'owner reçoit **une seule** alerte, « feuille de route épuisée pour
  <projet> », par la route de notification de l'owner.

Les relecteurs et les agents critiques ne sont jamais mis en sommeil par ce
geste. Le premier lot démarrable qui arrive réveille les agents en sommeil,
dans l'ordre normal de prise. #4610 : un agent à qui un lot actionnable est
attribué, ou qui reçoit un courrier non lu de son pilote ou d'un
orchestrateur, n'est pas endormi ; s'il l'était déjà, la pause est levée à
l'arrivée du lot ou du courrier, avec l'acteur et la raison au journal de la
pause. La fenêtre du courrier récent est `file.wake_mail_s` (secondes), à
défaut `file.repos_s` ; la même fenêtre s'applique au sommeil et à la levée
(#4619). `file.wake_mail_s` reste **facultative** : elle n'entre au canon
qu'une fois que tous les hôtes lisent ce code — un hôte d'une version
antérieure ignorerait la clé et garderait `file.repos_s`, changeant son
sommeil sans le savoir (#4619).

**La dernière note du pilote fait foi (#4623)** : une note
`roadmap: rouverte <projet>` postérieure à la dernière `roadmap: épuisée
<projet>` désarme le sommeil et lève les dormeurs du projet dans la même
passe ; le geste est journalisé sur le lot de découpage (auteur de la note,
note, agents levés). Une réouverture antérieure à la dernière note
d'épuisement ne change rien : `épuisée` → `rouverte` → `épuisée` endort de
nouveau. L'alerte `roadmap_exhausted` suit la même règle et disparaît dès la
réouverture. Le sommeil s'arrête aussi au classement du lot de découpage,
comme avant.

### 2.4 Acceptation de A

- Un projet sans lot démarrable, avec 20 % d'agents au repos depuis 30 min,
  reçoit un seul lot de découpage, assigné à son pilote.
- Sans lot démarrable créé dans l'heure, le passage suivant de l'auditeur crée
  les lots.
- Une feuille de route notée épuisée met les agents en sommeil, rend les hôtes
  burst et envoie une seule alerte à l'owner.
- Témoins :
  - aucun déclenchement sous 20 % ou sous 30 min ;
  - aucun déclenchement quand un lot démarrable existe ;
  - un seul lot de découpage par projet ouvert ;
  - #4610 : un lot actionnable attribué ou un courrier récent du pilote ou
    d'un orchestrateur retient l'agent, et la pause est levée à l'arrivée de
    l'un ou de l'autre (acteur et cause au journal des pauses) ;
  - `épuisée` → sommeil ; `épuisée` puis `rouverte` → aucun sommeil et levée
    des dormeurs dans la même passe ; `rouverte` puis `épuisée` → sommeil
    (#4623) ; l'alerte suit la dernière note.

## 3. Lot B : mesure de débit de l'auditeur **[owner]**

### 3.1 La mesure

`ameesh debit` gagne une section `capacity`, par projet et pour le mesh :

- `idle_agent_hours` sur la fenêtre du débit, et la part au repos des agents
  réveillables (`idle_share`) ;
- `startable_lots` ;
- `plan_loss_at_reset`, la part de fenêtre perdue au reset pour chaque compte
  de forfait, telle que `ameesh accounts` la calcule ;
- l'état du lot de découpage (§2), s'il est ouvert, et son âge.

`ameesh debit --check` sort 2 quand `idle_share` atteint ou dépasse 0,20
depuis 30 min et qu'aucun lot de découpage n'a moins d'une heure, comme pour
une attente de plus de 2 h sans geste.

### 3.2 La consigne de l'auditeur

- Le débit de capacité se lit en premier, avec les attentes.
- Un passage qui conclut « rien à signaler » avec 20 % ou plus d'agents au
  repos est un défaut de son travail, au même titre qu'une attente de plus de
  2 h.
- Le repli du §2.2 est un geste de niveau 1.
- Les rapports donnent les heures-agent au repos et le quota perdu de la
  dernière heure. L'analyse de nuit en donne la courbe sur 24 h.

### 3.3 Acceptation de B

- Avec 20 % d'agents au repos depuis 30 min sans lot de découpage récent,
  `debit --check` sort 2 et nomme le projet.
- La section `capacity` donne les heures-agent au repos et la perte au reset
  par compte.
- La consigne de l'auditeur (`docs/AUDITEUR.md`) et sa fiche de canon portent
  la règle.

## 4. Lot C : second paquet en parallèle **[owner]**

D'abord #4271 : tests d'unité en parallèle dans les slots `ameesh heavy`, carte
d'impact rafraîchie à la base, plafond par défaut compilé du canon.

Ensuite, une mesure journalière : sur la journée, le temps qu'un lot relu
attend l'ouverture de son paquet (`qa_merge`, du verdict OK à la composition).

- Si la **médiane dépasse 1 h**, ameesh compose un **second paquet en
  parallèle**.
- Il répartit les lots relus en deux paquets **indépendants** : les lots qui
  touchent les mêmes fichiers, ou dont l'un dépend de l'autre, restent dans le
  même paquet.
- Les deux paquets se testent en même temps et fusionnent l'un après l'autre.
  Le second est rebasé sur le premier fusionné, et refait seulement ses
  contrôles de gel quand le rebasage est sans conflit. S'il y a un conflit, il
  revient en file.
- Le seuil (`paquet.attente_mediane_s = 3600`) vient du bloc mécanique du
  canon. Le § 11 bis du canon passe de « une seule CI de paquet à la fois » à
  « au plus deux ». Le § 12 suit : la cible n'est plus réservée au premier
  paquet contre le second, et une cible bougée par le premier paquet, sans
  conflit, ne relance pas la CI du second (contrôles de gel seuls). La suite
  complète de `main` après chaque fusion reste le filet d'un conflit
  sémantique entre fichiers disjoints : rouge, on corrige en avant ou on
  revient en arrière (§ 11 bis).

### 4.1 Acceptation de C

- Avec une médiane d'attente au-dessus de 1 h la veille, ameesh compose deux
  paquets disjoints. Aucun lot ne figure dans les deux, et aucun fichier
  modifié n'est commun aux deux.
- Le second paquet fusionne après le premier sans refaire la suite complète
  quand le rebasage est sans conflit.
- Sous le seuil, il n'y a qu'un paquet.

## 5. Hors périmètre

- La vitesse d'un paquet : #4271.
- La libération des hôtes : #3167 (#4147, #4148).
- La limite de rafale Codex (`codex-short-window`, #3779) : à mesurer à part.
  Le 28/10, 6 agents Codex sur 6 étaient bloqués alors que 4 % de la semaine
  avait été consommé.
