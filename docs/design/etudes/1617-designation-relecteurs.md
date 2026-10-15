---
type: Study
status: in-progress
work_item: 1617
---

# Désignation automatique des relecteurs au gel

Le lot #1617 (sous-lot #1606 point 4, canon home §7) remplace le choix manuel
du relecteur par une sélection déterministe, au moment du gel. Constat du
15 octobre : dix-huit lots attendaient sans relecteur, jusqu'à 42 h. La
préparation ci-dessous est **locale et inactive** : aucun hook n'est branché,
aucun profil ni runner n'est modifié, aucune CI n'est déclenchée, et rien ne
désigne de relecteur en production tant que les revues et le raccord ne sont
pas acquis.

Sources : canon home PR86 §7 et #1606 point 4 ; politique versionnée PR85
(`review_policies.train_reviews`, schéma `ameesh-train-reviews-policy/1`) ;
mandat mesh-design du 15/10 10:10 ; preuves exactes #1465 ; lecteur
`train_policy.Versioned` et qualification `train_review.qualify` de #1467 ;
compilation du canon #1616.

## Périmètre livré : la sélection seule

`src/ameesh/review_designation.py` ne fait aucune E/S. Il reçoit des entrées
déjà lues et rend une décision :

- **politique** : des `Rule` (`paths`, `reviews` = 1 ou 2) déjà analysées. Le
  nombre requis est le **maximum** des règles qui couvrent les chemins du diff
  gelé ; un chemin qu'aucune règle ne couvre vaut zéro (règle du léger, PR85).
  Une déclaration mal formée est refusée — pas de repli permissif. La
  couverture des chemins non légers reste vérifiée par le lecteur (#1467).
- **candidats** : les lignes du registre, filtrées par équipe (l'`app` du lot).
  La famille est la famille **réelle de la session** (`registry.live_harness`,
  #1037/L242b) ; jamais le nom, jamais l'étiquette déclarée seule, jamais
  l'assignation. Un harnais hors des familles déclarées n'est pas une famille.
  La disponibilité écarte `stopped`/`paused`, les sessions `externe` (L46) et
  les pauses d'exploitation ou de budget (« paused… », « budget : … »). La
  charge est le nombre de lots ouverts de l'agent.
- **sélection** : `required` relecteurs (1 ou 2). Pour deux avis, les familles
  sont distinctes entre elles et chacune diffère de la famille de **chaque**
  auteur ou correcteur de la tête ; un auteur est écarté par son nom même si sa
  famille est inconnue. Un seul candidat par famille (le moins chargé), puis
  les familles les moins chargées ; à charge égale, l'ordre des noms tranche.
- **attente explicite** : s'il manque des familles indépendantes, l'état est
  `waiting` avec le nombre manquant, et une **notification de capacité** part
  vers le pilote — jamais une admission implicite.
- **courriers** : chaque relecteur reçoit le lot, le jalon `frozen` et le SHA
  **exact** à relire, la commande de verdict sur ce SHA, et la règle du §2
  (corriger soi-même un bloquant fait de lui un auteur : son avis ne compte
  plus, un autre relecteur reprend).

## Le contrat de raccord (à qualifier avec #1616 et #1467)

Le module est branché par un `Port` injecté, seule couche qui touche la base,
le registre et le courrier :

- `latest_freeze(lot)` : le dernier jalon `frozen` (`{id, sha}`) ;
- `recorded(lot, freeze_id)` : la désignation déjà écrite pour **ce** gel ;
- `commit(...)` : **une** transaction — verrou du lot, recontrôle que le gel
  examiné est toujours le dernier (sinon `StaleFreeze`, rien n'est écrit),
  marque idempotente par `(lot, jalon frozen)`, courriers déposés dans la même
  transaction.

Le moteur relit le dernier gel à chaque essai : un `StaleFreeze` le fait
repartir sur la nouvelle tête (au plus trois essais, puis échec à rejouer, sans
boucle). Un même gel rejoué rend la désignation enregistrée sans second
courrier ; un gel nouveau est un autre objet, donc une nouvelle désignation —
l'ancienne ne compte plus. Aucun retour à une ancienne tête n'est possible :
le gel est relu sous verrou, et un SHA qui n'est pas un identifiant de commit
est refusé.

Ce qui doit venir de l'extérieur, et de qui :

- **règles et familles** : de la compilation canon de #1616 (ou du lecteur
  `train_policy.Versioned` de #1467 en attendant) ; le repli local
  (`claude`, `codex`, `deepseek`, mêmes valeurs que `train_review.FAMILIES`)
  est un paramètre `families`, pas une constante de politique ;
- **required, base, tête exacte, classe** : du `Requirement` déjà calculé par
  #1467 (`train_policy.Versioned`) ;
- **auteurs et correcteurs** : des reçus exacts de #1465
  (`review_provenance.receipt_for_commit`), jamais d'une note ou d'une
  assignation ; l'assigné du lot reste un auteur au minimum.

## Le hook (non branché)

Le point d'appel naturel est l'écriture du jalon `frozen`
(`work.milestone(..., "frozen", sha=…)`, déjà sous le verrou de la ligne du
lot) ou l'étape de gel du runner de #1467, dans la **même transaction** que le
jalon. Le port Postgres sera écrit avec l'accord de #1616 et #1467 ; il
n'écrit ni `work.py` ni `review_lot.py` ni les modules `train*` sans cet
accord. La CLI (`ameesh work reviewers <lot>` ou équivalent) viendra au même
moment.

## Vérification

`tests/test_1617_review_designation.py` — 26 tests ciblés, sans base :
maximum des règles par chemin et zéro du léger ; deux familles SQL/sécurité/prod
distinctes et hors auteur ; correcteur devenu auteur écarté (nom et famille) ;
famille lue sur la session et non sur le nom ; indisponibilité et charge ;
attente explicite notifiée une fois ; politique Nexlink (deux avis `supabase`,
un par défaut) ; même gel rejoué = un seul courrier ; nouveau gel = nouvelle
désignation sans retour à l'ancienne tête ; gel qui bouge pendant l'écriture =
reprise sur la nouvelle tête, trois mouvements = échec sans boucle ; tête
invalide ou non gelée refusée.

## Reste avant activation

- accord de port avec #1616 et #1467, puis écriture du port Postgres ;
- branchement transactionnel au gel et commande de lecture ;
- preuve de livraison sur un **gel réel** : désignation sans agent, tête exacte
  remise, durée et doublons mesurés (le compteur de courriers par gel) ;
- deux avis indépendants hors auteur pour cette porte (sécurité de la chaîne
  d'admission), puis paquet à la coupure.
