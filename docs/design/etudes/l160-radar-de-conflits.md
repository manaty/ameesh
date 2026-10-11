---
type: Study
title: "Étude — radar de conflits entre lots, résolutions préparées avant la fusion, code écrit pour s'ajouter"
description: "Mesure des reprises de fusion du 10/10 (ameesh et Nexlink), ce que git permet déjà (merge-tree, rerere, file de fusion, pilote syntaxique, fragments), comparaison avec un suivi plus fin (bloc, symbole, CRDT, Pijul, jj), conception retenue pour ameesh et lots L160a–L160h estimés."
status: draft
tags: [fusion, conflits, git, rerere, merge-tree, file-de-fusion, conventions, radar]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-11T04:15:00+02:00" }
stale_after: 2027-01-11
sources:
  - { resource: "../../ORCHESTRATEUR.md", title: "Consigne des orchestrateurs (branche du lot, fusion sans PR, migrations réservées)" }
  - { resource: "../log.md", title: "Journal de la conception (L118, L125)" }
  - { resource: "../decisions/0018-vitesse-des-agents.md", title: "Décision 0018 (vitesse des agents, revue proportionnée)" }
  - { resource: "https://git-scm.com/docs/git-merge-tree", title: "git merge-tree --write-tree" }
  - { resource: "https://git-scm.com/docs/git-rerere", title: "git rerere" }
  - { resource: "https://git-scm.com/docs/rerere", title: "rerere, normalisation des conflits (documentation technique)" }
  - { resource: "https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue", title: "GitHub, file de fusion" }
  - { resource: "https://mergiraf.org/", title: "Mergiraf, pilote de fusion syntaxique" }
  - { resource: "https://docs.jj-vcs.dev/latest/conflicts/", title: "Jujutsu, conflits de première classe" }
  - { resource: "https://pijul.org/manual/theory.html", title: "Pijul, théorie des patchs" }
---

# Contexte

**Idée du propriétaire (11/10, 03:25)**, reprise telle quelle : « quand un agent
travaille sur sa branche de lot, un des problèmes de fusion est qu'on ne connaît
pas par avance l'ordre des fusions et les conflits potentiels à résoudre. Les
ajouts de fichiers ne sont jamais un problème (cela peut peut-être influencer la
façon dont on code) ; on pourrait imaginer que les agents modifiant un fichier
notifient les agents en écoute sur ce fichier, de sorte que les autres agents
puissent préparer plusieurs versions de leur modif avec ou sans la version de
l'autre agent. Ça suppose sans doute un système un peu différent de git, mais au
moment de la fusion on aurait déjà toutes les résolutions de conflit prêtes. »
Un système plus fin que le fichier.

**Constat de la nuit du 10/10** : les fusions de lots ameesh ont presque toutes
demandé une reprise (`docs/design/log.md` en tête de chaque PR,
`deploy/sql/role-superviseur.sql`, `ITEM_COLUMNS` de
`storage/postgres/work.py`, `storage/postgres/projects.py`, numéros de
migration 0048 à 0053 en collision). Côté Nexlink, l'orchestrateur sérialise les
fusions à la main, par des messages « feu de fusion ».

# Mesures

## Méthode

Tout en lecture seule : clones miroirs des deux dépôts dans un dossier jetable,
base du mesh en transaction `read_only`, PR et exécutions de CI par `gh`.

* **Fusions rejouées** : chaque commit de fusion à deux parents est refait par
  `git merge-tree --write-tree` ; un code de sortie 1 dit qu'il a fallu résoudre
  un conflit, et la liste des fichiers suit. Avec `merge.conflictStyle=diff3`,
  chaque bloc en conflit est classé : **ajout** (la base est vide : les deux
  côtés ont inséré au même endroit) ou **modification**.
* **Reflogs des worktrees** (un par agent) : « commit (merge) » est une fusion
  arrêtée sur conflit puis validée à la main ; « rebase (continue) » est une
  étape de rebase arrêtée puis reprise. Les rebases sont invisibles dans
  l'historique : seuls les reflogs les montrent. Les agents de la VM n'y
  figurent pas (autres clones).
* **Courrier** : messages de l'équipe Nexlink, classés par expression
  régulière (feu de fusion, develop frais, refetch, sans forcer, rebase,
  conflit, merge-tree…) ; délais entre le feu de l'orchestrateur et l'arrivée
  sur develop, appariés par l'étiquette du lot dans le titre du commit de
  fusion.

## ameesh, nuit du 10/10 (20 h → 3 h 20)

* **22 PR fusionnées**, dont 4 de version. Sur les **18 PR de lots, correctifs
  et consignes**, **11 ont demandé au moins une résolution à la main** et 2
  autres des rattrapages de `main` sans conflit (L63 : 2, L119 : 3). Seules
  les PR ouvertes et fusionnées en 15 min au plus y ont échappé. Délai entre
  ouverture et fusion : médiane 20 min.
* **19 résolutions à la main** en 7 h 20 : 10 fusions de `main` arrêtées (dont 3
  sur la branche d'intégration de la voie B) et 9 étapes de rebase reprises, dans
  11 des 13 worktrees qui ont fusionné ou rebasé.
* Fichiers en conflit dans ces 10 fusions : `docs/design/log.md` **8 fois sur
  10** ; `docs/EXPLOITATION.md`, `exploitation.py`, `notify.py` 4 fois ;
  `storage/interface.py`, `backend.py` 2 fois ; une fois
  `role-superviseur.sql`, `storage/postgres/work.py` (`ITEM_COLUMNS`),
  `storage/postgres/projects.py`, `cli.py`, `mesh_cli.py`, l'index des
  décisions, la référence de la CLI, la liste des modules « pilote psql », le
  numéro de version (`pyproject.toml`, `__init__.py` et son test) et six
  autres. 77 blocs en conflit, dont **35 ajouts au même endroit (45 %)** ; 4
  fusions sur 10 n'avaient que des ajouts.
* Sur tout l'historique public (depuis le 05/10) : 27 fusions en conflit sur 51,
  181 blocs, **92 ajouts (51 %)** ; les 12 blocs de `log.md` sont tous des ajouts.
* **Migrations** : le 10/10, le numéro 0048 a été pris par quatre lots et il y
  a eu 12 renumérotations, dont 9 pour les trois migrations de la voie B
  (trois fois chacune, jusqu'à 0051, 0052, 0053). Git n'y voit **aucun
  conflit** (noms de fichiers différents) : seul `migrations.discover`
  (« version de migration en double ») ou un test le révèle, après coup.
* La résolution textuelle est rapide (un rebase arrêté dure 30 s en médiane) :
  le coût est ailleurs, dans le tour d'agent qu'elle ouvre, la CI relancée
  (3,6 min en médiane, 36 commits testés pour 22 PR) et l'attente des lots
  suivants.
* `rerere` est **déjà actif** sur le poste (configuration globale,
  `rerere.autoUpdate`) : 153 conflits enregistrés pour ameesh (142 résolus),
  dont 111 les 10 et 11/10, et 343 pour Nexlink. Il ne sert qu'après coup, et
  seulement sur le poste.

## Nexlink, journée du 10/10 (12 h → 4 h)

* **develop** a reçu 275 commits de premier parent (classés par leur titre) :
  **75 fusions de lots**, **29 fusions de rattrapage** (develop a bougé pendant
  la fusion d'un autre : « intègre develop avant le push »), 129 notices et
  rapports de revue, 18 commits de réservation de numéros de migration, 24
  autres.
* Conflits textuels rares : 20 fusions validées à la main et 4 étapes de rebase
  reprises dans les reflogs de 74 worktrees ; 10 fusions en conflit au
  rejeu, dont 5 sur le registre des réservations de migrations. Sur 15 jours :
  6 % des fusions dans develop et 10 % des fusions de develop dans une branche,
  **53 % des blocs sont des ajouts** au même endroit ; fichiers chauds : les
  trois fichiers de traduction JSON, le registre des réservations, le journal de
  connaissances, la feuille de route, les fiches de suivi des chantiers. Les
  agents font déjà des **fusions d'essai** par `merge-tree` avant de fusionner,
  à la main.
* **Le coût est la sérialisation** :
  * entre le premier feu de l'orchestrateur et l'arrivée sur develop :
    **médiane 39 min, 3e quartile 76 min, maximum 3 h 08** (33 fusions
    appariées) ; entre le dernier GO de revue et la fusion : 8 min en médiane,
    à peu près la durée d'une fusion quand le lot est en tête ;
  * file tenue à la main : « feu donné, position 7 de la file » ;
  * de 19 h à 23 h, **382 messages sur 1 345 (28 %)** parlent de fusion ;
    l'orchestrateur en écrit 76 sur 332 ; « feu de fusion » apparaît
    littéralement dans 35 messages de la journée.

## Ce qu'un radar aurait vu

Fusions d'essai entre branches actives, mesurées sur l'état présent :

| | Branches | Paires | Paires qui touchent un même fichier | Paires en conflit | En conflit avec la cible | Passage complet |
|---|---|---|---|---|---|---|
| Nexlink (poussées depuis le 10/10 12 h) | 62 | 1 891 | 43 (2,3 %) | 31 | 12 | 9 s |
| ameesh (21 PR ouvertes, surtout la pile v2) | 21 | 210 | 112 (53 %) | 72 | 16 | 3 s |

Une fusion d'essai coûte 0,02 à 0,07 s par paire. Chez Nexlink, les paires qui
se recouvrent sont rares : un radar préviendrait peu, et à bon escient. Chez
ameesh, presque tout se recouvre à cause d'une poignée de fichiers centraux
(`EXPLOITATION.md` 69 paires, `main.py` 57) : il faut **d'abord** changer ces
fichiers, sinon le radar crie sans cesse.

# 1. Ce que git permet déjà

## Fusions d'essai sans worktree

`git merge-tree --write-tree A B` (git ≥ 2.38, `--merge-base` ≥ 2.40) calcule
la fusion sans index ni dossier : il rend l'arbre fusionné, la liste des
fichiers en conflit et sort en 1 s'il y en a. Il n'écrit que des objets ; avec
`GIT_OBJECT_DIRECTORY` pointé sur un dossier jetable et le dépôt en
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, il n'écrit **rien** dans le dépôt des agents
(vérifié). La granularité est déjà **le bloc** : deux lots qui modifient des
fonctions différentes d'un même fichier ne sont pas en conflit. Limite : il ne
voit que les conflits textuels ; deux migrations 0048, une fonction supprimée
d'un côté et appelée de l'autre passent.

**Verdict** : la brique du radar.

## Résolutions enregistrées et rejouées (`rerere`)

`rerere` note chaque conflit résolu (côtés en conflit et résultat) et rejoue la
résolution quand le même conflit revient. Le conflit est identifié par ses deux
côtés **rangés dans un ordre fixe** : la même résolution sert que A fusionne
avant B ou l'inverse. C'est exactement « avec ou sans la version de l'autre »
pour une paire de lots. Le cache `rr-cache` est dans le dossier commun du
dépôt, donc partagé par toutes les worktrees d'un hôte.

Limites : il n'apprend qu'**après** une résolution ; il ne passe pas d'un hôte
à l'autre ; si un troisième lot a changé les mêmes lignes, le conflit est
nouveau ; il peut rejouer une résolution devenue fausse sans rien dire
(`autoUpdate`), d'où les portes sur l'arbre fusionné ; ses entrées expirent
(`gc.rerereResolved`, 60 jours).

Partage entre agents, deux façons :

* copier les fichiers de `rr-cache` dans une ref dédiée : format interne de git,
  fragile ;
* **partager des commits de résolution** : l'agent fusionne la branche de
  l'autre dans une branche jetable issue de la sienne, résout, valide ; ce
  commit R(A, B) est poussé sous une ref dédiée. N'importe quel hôte en
  réapprend la résolution avec `rerere-train.sh` (livré avec git, `contrib/`),
  qui rejoue des fusions pour remplir le cache. Un commit est natif, poussable
  sur GitHub, relisible et testable.

**Verdict** : retenu, par commits de résolution.

## File de fusion avec empilement testé

Une file range les lots prêts et teste chacun fusionné **sur la cible plus les
lots qui le précèdent** (« merge train ») ; un lot qui casse sort de la file
sans bloquer les autres. GitHub la propose (`merge_group`) pour les dépôts
publics d'organisation, donc ameesh, et pour les dépôts privés seulement en
GitHub Enterprise Cloud : pas Nexlink, au forfait gratuit, qui fusionne en
local de toute façon. Équivalents : trains de GitLab, Zuul, Mergify. Une file ne
résout aucun conflit : elle retire le lot.

**Verdict** : principe retenu ; la file est tenue par ameesh, la même pour une
équipe à PR et pour une équipe qui fusionne en local.

## Pilote de fusion syntaxique

Mergiraf fusionne par arbre syntaxique (tree-sitter) : deux ajouts dans un
ensemble dont l'ordre ne compte pas (imports, membres d'une classe, clés d'un
objet JSON, selon ce que déclare chaque langage) ne sont plus en conflit. Il
comprend Python, TypeScript, JSON, YAML et TOML, mais ni SQL ni Markdown, et
rien à l'intérieur d'une chaîne (`ITEM_COLUMNS` est une chaîne SQL). Il
s'installe comme pilote git :

```
git config merge.mergiraf.driver "mergiraf merge --git %O %A %B -s %S -x %X -y %Y -p %P -l %L"
echo '*.py merge=mergiraf' >> .gitattributes
```

Ce qu'il ne sait pas fusionner reste en conflit, comme avant. GitHub ne lance
aucun pilote externe : il ne vaut que pour les fusions faites en local, ce qui
est le cas des reprises. Le pilote `union` intégré à git garde les deux côtés
d'un fichier fait de lignes indépendantes, au risque de les entremêler.
`merge.conflictStyle=zdiff3` montre la base dans les marqueurs et aide l'agent
qui résout.

**Verdict** : pilote à mesurer sur les 103 fusions en conflit rejouées avant
de l'adopter ; jamais pour SQL ni Markdown.

## Fragments plutôt que fichiers centraux

C'est l'intuition du propriétaire : un fichier ajouté n'entre jamais en conflit.
Un journal fait d'un fragment par lot, assemblé à la lecture (comme
towncrier, changesets ou reno), un index généré depuis l'en-tête des fiches, un
registre par dossier rempli par découverte, un numéro réservé à l'avance
suppriment la classe de conflit au lieu de la résoudre. Ils couvrent les
fichiers les plus chauds des deux dépôts : `log.md`, l'index des décisions, les
fichiers de traduction, le registre des réservations, le journal de
connaissances. Deux réserves : deux lots qui créent le **même chemin** sont en
conflit, et deux numéros égaux dans deux fichiers différents passent sans
conflit ; il faut une sonde. Et réorganiser un fichier central entre en conflit
avec toutes les branches ouvertes : à faire file vide, annoncé.

**Verdict** : le gain le plus sûr ; à faire en premier.

## Ce qu'ameesh sait déjà

* Un lot porte sa branche, sa cible et le dernier commit vu en avance
  (`branch`, `branch_target`, `branch_head`, L118, migration 0050).
* L'exécuteur de chaque hôte relève toutes les 5 min les branches des lots de
  ses agents (`plan_git.sync_branches`, `AMEESH_BRANCH_SWEEP_INTERVAL`) dans le
  dossier de travail de l'assigné, et constate la fusion par le contenu
  (ancêtre, patch-id, squash, ligne `ameesh-work:`).
* Le courrier se rattache à un lot (`--lot`) ; un message passif (`ack`, `cc`,
  `broadcast`) ne réveille personne et se lit au tour suivant (L125) ; un
  `event` réveille au plus une fois par fenêtre de 120 s.
* Il manque : les fichiers touchés par lot, les paires qui se recouvrent, l'ordre
  de fusion, les résolutions.

# 2. Un suivi plus fin que git ?

| Approche | Gain réel | Coût | GitHub et PR |
|---|---|---|---|
| Écoute **par fichier** | Prévient tôt | Bruit : 53 % des paires ameesh se recouvrent | Indifférent |
| **Au bloc** (fusions d'essai) | Le conflit réel, rien de plus | Faible : 9 s par passage | Compatible |
| **Au symbole** (pilote syntaxique) | Les ajouts dans une même liste ou classe ne sont plus des conflits | Un binaire par hôte, limité à certains langages | Compatible en local |
| **CRDT** | Pas de marqueurs : deux insertions concurrentes sont rangées d'office | Le code peut être faux sans que rien le dise ; journal d'opérations à tenir ; fait pour la co-édition en direct, pas pour des branches de plusieurs heures | Incompatible |
| **Pijul** | Patchs qui commutent : une résolution s'applique dans n'importe quel ordre, l'idée du propriétaire en natif | Autre système, autre hébergement, outils, harnais et CI à refaire | Incompatible |
| **Jujutsu (jj)** | Conflits de première classe : un rebase ne s'arrête jamais, la résolution suit les descendants ; utile pour les piles de lots | Stocké dans git et poussé sur GitHub, mais un commit en conflit ne doit pas être poussé ; les harnais parlent git ; ne prévient personne | Compatible |

La granularité utile est **le bloc**, et git la donne déjà par les fusions
d'essai. Le symbole n'apporte que la fusion automatique des ajouts, qu'un pilote
syntaxique ou une convention donnent aussi. Aucun système ne supprime les
conflits **sémantiques** : seules les portes sur l'arbre fusionné, dans l'ordre
de fusion, les attrapent. Changer d'outil coûterait la compatibilité avec
GitHub, les PR et les harnais, pour un gain que git et ameesh peuvent donner.
jj reste à essayer plus tard pour les longues piles de lots (pile v2).

# 3. Conception retenue

Quatre pièces, dans l'ordre où elles rapportent : conventions additives,
radar, résolutions préparées, file et fusion par ameesh.

## 3.1 Radar

* **Où** : l'exécuteur de l'hôte désigné du projet (réglage `radar.projects`,
  comme `github.projects`), juste après le relevé des branches. Il travaille
  dans un **clone privé** du projet (`--shared`, objets du dépôt en
  alternates : ni copie, ni écriture dans le dépôt des agents), avec ses propres
  refs, son propre `rr-cache` et des worktrees jetables. Il lit les branches
  locales du dépôt commun de l'hôte, qui sont celles de toutes les worktrees, et
  `origin` pour les agents des autres hôtes. Règle proposée (question 3) : ces
  agents poussent leur branche de lot à chaque fin de tour qui a créé un
  commit ; le radar ne voit que ce qui est commité.
* **Quand** : à chaque passage du relevé (5 min), et seulement pour ce qui a
  changé : une paire n'est recalculée que si l'une de ses deux pointes ou la
  cible a bougé (cache par triplet de commits). Un passage complet coûte 9 s sur
  le plus gros projet.
* **Quoi**, pour les lots ouverts qui ont une branche :
  1. les fichiers touchés depuis la base commune avec la cible ;
  2. les paires qui touchent un même fichier, seules soumises à
     `merge-tree` (un conflit d'une paire), plus chaque lot contre la cible ;
  3. des **sondes** pour ce que git ne voit pas, déclarées par projet : numéros
     de migration en double (ameesh `src/ameesh/migrations/NNNN_`, Nexlink son
     dossier de migrations), même chemin créé deux fois, numéro réservé par un
     autre lot ;
  4. le résultat en base (paire, commits, fichiers, blocs, état `clean`,
     `overlap`, `conflict`, `prepared`, `stale`), migration réservée au début du
     lot.
* **Écoute** : implicite, chaque lot écoute les fichiers de son propre diff ;
  pas d'abonnement à tenir. En option, `ameesh work watch <lot> <chemin>`
  déclare un fichier qu'on va toucher, pour être prévenu avant d'avoir commité
  (fichier central, numéro de migration).
* **À qui et comment**, sans réveil inutile (L125) :
  * aux deux assignés de la paire, par un courrier **lié au lot** (`--lot`),
    passif (nouvelle clé `radar` dans les clés passives) : lu au tour suivant,
    jamais de tour ouvert pour lui ; aussi dans le fil du lot et `ameesh work
    show` ;
  * seulement sur **changement d'état** (nouveau conflit, résolution périmée,
    autre lot fusionné), une fois par paire et par couple de commits ;
  * un réveil (courrier ordinaire, regroupé 90 s) seulement quand le conflit
    **bloque** : l'autre lot est fusionné ou en tête de file et la résolution
    n'est pas prête ; jamais `--urgent` ;
  * l'orchestrateur ne reçoit pas de courrier par paire : une ligne dans
    `ameesh projects` (« conflits prévus : n, préparés : p ») et l'alerte
    `merge_blocked` quand la tête de file attend une résolution depuis 30 min ;
  * jamais d'humain, sauf par cette alerte.

## 3.2 Résolution préparée par l'agent concerné

* **Qui** : pour chaque paire en conflit, un seul **résolveur**, celui des deux
  lots qui fusionnera en second dans l'ordre prévu (3.3) ; à défaut, celui
  dont le diff est le plus petit dans les fichiers en conflit. Le radar le dit
  dans son courrier.
* **Comment** : `ameesh work resolve <lot> --with <autre lot>` ouvre une
  worktree jetable issue de la branche du lot, y fusionne la branche de l'autre
  et laisse l'agent résoudre ; à la validation, le commit de résolution
  (trailer `ameesh-resolution: <lot> <autre>`) est poussé sous une ref dédiée
  (`refs/ameesh/resolutions/<lot>/<autre>`, ou une branche
  `ameesh/resolve/<lot>-<autre>` si l'hébergeur refuse les refs hors
  `refs/heads`) et enregistré en base. La branche du lot ne change pas : elle
  reste la version **sans** l'autre, le commit de résolution est la version
  **avec**.
* **Validité** : le radar rejoue la fusion de la paire dans son clone, cache
  amorcé par `rerere-train.sh` sur les commits de résolution ; si tout le
  conflit est rejoué, la paire est `prepared`. Une nouvelle poussée qui change
  les blocs en conflit la rend `stale`, et le résolveur est prévenu (passif).
  Un rebase ne la périme pas tant que les blocs en conflit restent les mêmes :
  `rerere` reconnaît le contenu, pas les commits.
* **Quand** : au fil du travail, au tour suivant, pas au moment de la fusion :
  l'agent connaît encore son code, et personne n'attend.

## 3.3 Ordre de fusion choisi par ameesh

* Une file par cible (dépôt et branche), en base. Un lot y entre quand il est
  prêt : gel, GO de revue, CI verte, accord du propriétaire si la règle
  l'exige ; par `ameesh work queue add <lot>` de l'orchestrateur, ce qui
  remplace le « feu de fusion ». Un lot en file est gelé : son agent n'y pousse
  plus sans l'en retirer.
* **Ordre** : priorité d'abord (règle « fusionner d'abord ce qui débloque le
  propriétaire »), puis les lots sans conflit, puis ceux dont les conflits sont
  préparés, enfin l'arrivée. Un lot dont un conflit avec un lot qui le précède
  n'est pas préparé recule d'un rang, et son résolveur est réveillé : le
  conflit est devenu bloquant.
* **Essai empilé** : avant chaque fusion, la cible plus les lots qui précèdent
  plus celui-ci, résolutions rejouées ; un conflit non couvert sort le lot de la
  file au lieu de bloquer les suivants.

## 3.4 Fusion qui rejoue les résolutions

`ameesh work land <lot>`, lancé par l'exécuteur (pas par un tour d'agent) pour
la tête de file :

1. **verrou par cible** : une seule fusion à la fois, ce qui supprime les
   fusions de rattrapage (29 chez Nexlink le 10/10) ;
2. dans le clone du radar : `fetch`, worktree jetable sur la cible fraîche,
   cache amorcé par les commits de résolution de ce lot ;
3. fusion : `git merge --no-ff` du gel avec la ligne `ameesh-work: <id>`
   (équipe qui fusionne en local), ou fusion de la cible dans la branche de la
   PR (équipe à PR) ; `rerere` rejoue ; s'il reste un conflit, arrêt, le lot
   sort de la file et son agent est réveillé ;
4. **portes du projet** sur l'arbre fusionné (commande déclarée par projet :
   tests ciblés, suites SQL, compilation) ; échec : même traitement ;
5. poussée sans forcer ; PR : fusion par l'API une fois la CI verte ;
6. notice dans le fil du lot (résolutions rejouées, auteurs, portes), lot fermé
   par le relevé (L118), radar recalculé pour la file, lot suivant.

Une fusion préparée ne demande ainsi **aucun tour d'agent**. Les gestes en
production et les étiquettes de version restent hors de la file : ils suivent
les règles de livraison d'ORCHESTRATEUR.md.

## 3.5 Conventions « additives » à imposer dans les consignes

Pour ameesh (CONTRIBUTING.md, consignes des agents) et pour Nexlink (sa
consigne d'équipe) :

1. **Ajouter un fichier plutôt que modifier un fichier partagé** : un module,
   un test, une page de documentation par sujet.
2. **Journaux en fragments** : un fichier par lot
   (`docs/design/log.d/2026-10-11-l160.md`), assemblé par un script vérifié en
   CI ; on n'écrit plus en tête de `log.md`. De même pour le journal de connaissances et la feuille
   de route de Nexlink.
3. **Index générés** depuis l'en-tête des fiches (index des décisions, des
   études), jamais tenus à la main.
4. **Listes** : un élément par ligne, virgule finale, rangées par ordre
   alphabétique ou dans le groupe du sujet, jamais plusieurs éléments sur une
   ligne (`ITEM_COLUMNS`, colonnes accordées par `role-superviseur.sql`).
5. **Registres par découverte** : une alerte, une commande, une sonde se
   déclarent dans leur propre module, pas dans une liste centrale (`main.py`,
   `mesh_cli.py`, `exploitation.py`, `notify.py`).
6. **Fichiers SQL de contrat découpés** par table ou par lot, assemblés dans
   l'ordre ; traductions découpées par espace de noms (un fichier par
   fonctionnalité et par langue).
7. **Numéros réservés auprès d'ameesh** au début du lot (`ameesh work reserve
   migration`, compteur en base, rattaché au lot), jamais « le suivant » ni un
   commit de réservation sur la branche commune ; la sonde du radar vérifie.
8. **Pas de remise en forme hors sujet** : déplacer ou reformater du code
   partagé est un lot à part, annoncé par le radar et fusionné file vide.
9. **Commiter et pousser souvent** : le radar ne voit que les commits.

# 4. Lots de mise en œuvre

Lettres plutôt que numéros, pour ne pas prendre de numéros que d'autres lots
réservent (précédent : L31b, L73b). Migrations : réservées au début du lot qui
en a besoin (règle 5 des orchestrateurs). Les durées sont du travail d'agent,
revue comprise ; les dates supposent un agent ameesh disponible.

| Lot | Contenu | Dépend de | Durée estimée | Date proposée |
|---|---|---|---|---|
| **L160a** — conventions et fragments ameesh | journal en fragments (`log.d/`, assemblage, contrôle CI), index des décisions et des études générés, `ITEM_COLUMNS` et listes une entrée par ligne, `merge.conflictStyle=zdiff3` posé par `ameesh doctor`, conventions dans CONTRIBUTING.md et les consignes ; fait file vide | — | 3 h | 12/10 |
| **L160b** — radar en observation | clone privé par projet (objets partagés), passage après le relevé des branches, fichiers par lot, paires, `merge-tree`, sonde des migrations, table des paires (1 migration), `ameesh work radar`, ligne dans `ameesh projects` ; aucun courrier pendant 24 h, pour calibrer | — | 5 h | 12/10 |
| **L160c** — courrier du radar | clé passive `radar` (L125), courrier lié au lot sur changement d'état, réveil seulement quand le conflit bloque, fil du lot, `work show`, `work watch` | L160b | 2 h | 13/10 |
| **L160d** — résolutions préparées | `ameesh work resolve`, commits de résolution sous `refs/ameesh/resolutions/`, amorçage par `rerere-train.sh`, états `prepared` et `stale`, choix du résolveur | L160b | 5 h | 13/10 |
| **L160e** — file et fusion par ameesh | file par cible (1 migration), ordre, essai empilé, `ameesh work queue`, `ameesh work land` (verrou, rejeu, portes déclarées, poussée sans forcer, PR par l'API), alerte `merge_blocked`, ORCHESTRATEUR.md sans « feu de fusion » | L160d | 6 h | 14/10 |
| **L160f** — Nexlink | conventions dans sa consigne, traductions et registre des réservations découpés, journal en fragments, portes déclarées, file ameesh à la place du feu de fusion ; pilote une journée | L160e, accord de l'orchestrateur Nexlink | 4 h et 1 jour de pilote | 15/10 |
| **L160g** — pilote syntaxique | mergiraf rejoué sur les 103 fusions en conflit mesurées ; adoption par langage (Python, TypeScript, JSON) seulement si aucune résolution fausse | — | 2 h | 13/10 |
| **L160h** — mesure du gain | scripts de mesure de cette étude dans `scripts/` (rejeu, reflogs, courrier), rapport à J+7 | L160e | 1 h | 19/10 |

Total : 28 h de travail d'agent et une journée de pilote. Ordre conseillé :
L160a et L160b le même jour, puis L160c, L160d et L160g, puis L160e, puis
L160f ; L160h une semaine après.

## Gain attendu

Base : les mesures ci-dessus, refaites par L160h avec les mêmes scripts.

| Mesure | Avant (10/10) | Après L160a | Après L160e (L160f pour Nexlink) |
|---|---|---|---|
| ameesh : PR qui demandent une résolution à la main au moment de la fusion | 11 sur 18 | environ 7 (−40 % : les fusions qui n'avaient que des ajouts disparaissent, 4 sur 10 cette nuit-là, 9 sur 27 depuis le 05/10) | 2 au plus (conflits non préparés) |
| ameesh : résolutions à la main sur une nuit de ce type | 19 | environ 11 | 3 au plus au moment de la fusion ; les autres préparées au fil du travail |
| Nexlink : fusions de rattrapage | 29 en 16 h | inchangé | 0 (verrou par cible) |
| Nexlink : délai du feu à develop | médiane 39 min, 3e quartile 76 min | inchangé | médiane 10 min, 3e quartile 25 min (portes et poussée) |
| Nexlink : messages sur la fusion | 382 sur 1 345 en 4 h (28 %) | inchangé | moins de 150 (restent les rapports de revue) |
| Nexlink : messages de fusion de l'orchestrateur | 76 en 4 h | inchangé | moins de 10 (entrée en file, exceptions) |
| Coût du radar | — | — | moins de 10 s de calcul toutes les 5 min sur le plus gros projet |

Hypothèses : la moitié des blocs en conflit sont des ajouts, et les
conventions les suppriment au lieu de les résoudre ; les conflits restants sont
préparés au tour suivant de leur résolveur, avant que l'autre lot fusionne, dans
neuf cas sur dix ; la fusion d'un lot en tête de file prend le temps de ses
portes (8 min mesurées). Les conflits sémantiques ne baissent pas ; ils
continuent d'être pris par les portes sur l'arbre fusionné, désormais dans
l'ordre réel de fusion.

## Garde-fous

* Une résolution rejouée peut être fausse : portes du projet sur l'arbre
  fusionné à chaque fusion, résolution attribuée à son auteur dans la notice,
  commit relisible.
* Le radar ne fusionne jamais rien : il calcule dans son clone et prévient.
* Une équipe qui n'a pas déclaré ses portes garde sa fusion à la main : la file
  ordonne, l'agent fusionne.
* Une fusion à la main reste possible en urgence (humain, orchestrateur) : elle
  passe, et le radar recalcule la file.
* Le dépôt public ameesh ne reçoit en refs de résolution que du code déjà
  public par les branches des PR.

# Questions au propriétaire

1. ameesh garde-t-il la fusion par squash de GitHub ? La fusion par ameesh
   intègre alors `main` dans la branche de la PR avant de la fusionner par
   l'API. La file de GitHub, disponible pour ce dépôt public, n'est pas
   nécessaire tant que la CI dure moins de 5 min.
2. Nexlink confie-t-il l'exécution de ses fusions à ameesh (portes lancées par
   l'exécuteur, pas par un agent) ? Sans cela, L160f se limite à l'ordre et aux
   résolutions préparées.
3. Les agents des autres hôtes poussent-ils leur branche de lot à chaque fin de
   tour qui a créé un commit ? Sans cela, le radar ne voit leur travail qu'au
   gel.
