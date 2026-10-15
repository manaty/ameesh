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

## Le port Postgres (`src/ameesh/review_designation_pg.py`)

Le `Port` est écrit, avec deux usages selon qui tient la transaction :

- **au gel** (hook étroit, à brancher) : `PgPort(tx, after_commit=threads)` —
  `commit` n'ouvre aucune transaction, écrit dans celle du jalon `frozen` et
  laisse l'appelant vider `threads` après son commit ;
- **hors hook** (reprise, CLI) : `PgPort(db)` — `commit` ouvre sa propre
  transaction, verrouille la ligne du lot (`FOR NO KEY UPDATE`, le même verrou
  que `add_milestone`), puis vide lui-même le fil après le commit.

La marque d'idempotence est une ligne du journal du lot, préfixée par le lot
et le jalon — `review-designation:<lot>:<gel>: {json}` — qui porte l'état, le
nombre requis, les relecteurs, le SHA exact et les auteurs du contenu gelé.
`recorded` la relit (la dernière ligne de ce gel fait foi) : un même gel
rejoué ne renvoie pas de second courrier, une attente ne se renotifie pas,
mais elle **cède** la place à la désignation quand la capacité revient, sur le
même gel. Une marque illisible est refusée, jamais interprétée. Les courriers
partent dans la même transaction (`mail.send(..., after_commit=…)`, motif
#315) : un envoi annulé ne laisse ni marque, ni message, ni trace de fil. Un
destinataire hors du registre est refusé avant toute écriture : un courrier
qui ne réveillerait personne ne vaut pas une désignation.


Une désignation est **finale** pour son gel ; une attente est un **marqueur
réessayable** : une notification par gel, et si la capacité revient — ou si la
preuve d'auteur arrive — la désignation remplace le marqueur pour le même gel,
les courriers aux relecteurs ne partant qu'alors, une seule fois. Une
provenance d'auteur inconnue (#1465 manquant pour un commit du gel) ne se
devine pas : la désignation passe en attente avec une notification, jamais un
relecteur choisi au hasard.

Ce qui doit venir de l'extérieur, et de qui :

- **règles et familles** : de la compilation canon de #1616 (ou du lecteur
  `train_policy.Versioned` de #1467 en attendant) ; le repli local
  (`claude`, `codex`, `deepseek`, mêmes valeurs que `train_review.FAMILIES`)
  est un paramètre `families`, pas une constante de politique ;
- **required, base, tête exacte, classe, commit de canon** : de l'adaptateur
  au gel, à partir du `Requirement` déjà calculé par #1467
  (`train_policy.Versioned`) — cx3 a confirmé le 15/10 que le runner de #1467
  ne voit pas le gel à l'écriture et que le raccord se fait par un hook étroit
  au gel, pas dans `train*` ;
- **auteurs et correcteurs** : des reçus exacts de #1465
  (`review_provenance.receipt_for_commit`), jamais d'une note ou d'une
  assignation ; l'assigné du lot reste un auteur au minimum. La collecte doit
  fonctionner **avant tout verdict** : `train_review.qualify` attend les avis
  et n'est pas appelable ici. Une preuve manquante est passée en
  `unknown_authors` (attente), jamais ignorée.

## Le hook (non branché)

Point d'accord avec cx3 (#1467, 15/10 10:21) : un hook **étroit** sur
`work.milestone(kind=frozen)`, sans modifier les modules `train*` ni coder de
politique en dur. L'écriture du jalon tient déjà le verrou de la ligne du lot
(`storage.postgres.work.add_milestone`) ; le raccord doit faire tenir dans la
**même transaction** la marque idempotente, les courriers et le journal, le
fil lisible étant écrit après le commit (`mail.send(..., after_commit=…)`).
La signature du `Port` ci-dessus est la surface à qualifier avec #1616
(adaptateur canon) et #1467 (point d'appel) ; ni `work.py` ni `review_lot.py`
ni `train*` ne sont touchés avant cet accord. La CLI
(`ameesh work reviewers <lot>` ou équivalent) viendra au même moment.

## Vérification

`tests/test_1617_review_designation.py` — 28 tests ciblés, sans base :
maximum des règles par chemin et zéro du léger ; deux familles SQL/sécurité/prod
distinctes et hors auteur ; correcteur devenu auteur écarté (nom et famille) ;
famille lue sur la session et non sur le nom ; indisponibilité et charge ;
attente explicite notifiée une fois ; provenance d'auteur inconnue = attente,
puis désignation quand la preuve arrive ; capacité revenue = désignation sur le
même gel ; politique Nexlink (deux avis `supabase`, un par défaut) ; même gel
rejoué = un seul courrier ; nouveau gel = nouvelle désignation sans retour à
l'ancienne tête ; gel qui bouge pendant l'écriture = reprise sur la nouvelle
tête, trois mouvements = échec sans boucle ; tête invalide ou non gelée
refusée.

`tests/test_1617_review_designation_pg.py` — 15 tests sur base réelle (psql) :
désignation écrite et courriers partis une fois, rejeu sans second courrier ;
un second écrivain (autre connexion) sous le verrou rend la marque de la base
sans remailer ; gel périmé refusé sans rien écrire (ni marque ni message) ;
reprise du moteur sur la nouvelle tête ; attente notifiée une seule fois puis
désignation sur le même gel ; attente sans destinataire refusée ; destinataire
inconnu du registre refusé (un courrier qui ne réveille personne) ; provenance
d'auteur inconnue = attente, désignation quand la preuve arrive ; nouveau gel =
nouvelle désignation, la marque de l'ancien gel reste attachée à sa tête ;
transaction du hook (fil vidé après commit seulement) ; annulation = ni
courrier ni marque.

## Reste avant activation

- accord de port avec #1616 (adaptateur canon : règles, familles, `policy_ref`)
  et #1467 (point d'appel) — cx3 a donné le 15/10 son accord pour un hook
  étroit sur `work.milestone(kind=frozen)`, impl07 n'a pas encore répondu ;
- branchement transactionnel au gel (callback après l'insertion du jalon dans
  `add_milestone`) et commande de lecture ; l'adaptateur fournit aussi le
  pilote à notifier (`notify`) : aucune source machine unique du pilote de
  projet n'existe aujourd'hui, et le port refuse un destinataire vide ou hors
  du registre ;
- preuve de livraison sur un **gel réel** : désignation sans agent, tête exacte
  remise, durée et doublons mesurés (le compteur de courriers par gel) ;
- deux avis indépendants hors auteur pour cette porte (sécurité de la chaîne
  d'admission), puis paquet à la coupure.
