---
type: Study
status: in-progress
work_item: 1617
---

# Désignation automatique des relecteurs au gel

Le lot #1617 (sous-lot #1606 point 4, canon home §7) remplace le choix manuel
du relecteur par une sélection déterministe, au moment du gel. Constat du
15 octobre : dix-huit lots attendaient sans relecteur, jusqu'à 42 h. La
préparation ci-dessous est **locale et inactive** : le hook étroit du gel est
branché mais aucun appelant ne fournit de callback, aucun profil ni runner
n'est modifié, aucune CI n'est déclenchée, et rien ne désigne de relecteur en
production tant que les revues et le paquet ne sont pas acquis.

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
  auteur ou correcteur de la tête ; un auteur dont la famille réelle est
  inconnue (vide ou étiquette) fait **attendre** la désignation — l'écarter par
  son seul nom laisserait passer un relecteur de sa famille réelle (B3, revue
  Claude dev1). Un seul candidat par famille (le moins chargé), puis les
  familles les moins chargées ; à charge égale, l'ordre des noms tranche.
- **attente explicite** : s'il manque des familles indépendantes, ou qu'une
  famille d'auteur est inconnue, l'état est `waiting` avec le nombre manquant,
  et une **notification de capacité** part vers le pilote — jamais une
  admission implicite.
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

Le gel est relu **une fois**, et les chemins, auteurs et la classe fournis sont
ceux de ce gel : si le `Port` refuse un gel devenu périmé (`StaleFreeze`, rien
n'est écrit), `apply` lève `FreezeMoved` et ne désigne **pas** la nouvelle tête
avec les entrées de l'ancienne (un SHA de courrier changé ne suffit pas) —
l'appelant recollecte les entrées du nouveau gel, puis rejoue (B1, revue Codex
cx2). L'appelant nomme ce gel (`freeze_sha`, le SHA du jalon sur lequel il a
collecté) : un gel arrivé **avant** l'appel, entre la collecte et la lecture,
est refusé de même par `FreezeMoved`, avant toute sélection (R Claude dev2).
Un même gel rejoué rend la désignation enregistrée sans second courrier ;
un gel nouveau est un autre objet, donc une nouvelle désignation — l'ancienne
ne compte plus. Aucun retour à une ancienne tête n'est possible : le gel est
relu sous verrou, et un SHA qui n'est pas un identifiant de commit est refusé.

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
  `train_policy.Versioned` de #1467 en attendant) ; impl07 a livré le 15/10 le
  contrat `mechanics.load(repo, commit, project)` (tête `ddf1091d`, schéma
  `ameesh-train-reviews-policy/1`) : `.rules` → `[(r.paths, r.reviews) …]`
  mappés en `Rule`, `.families` + `.exclude_author_family` +
  `.distinct_families` pour l'indépendance, `.reviews_for(paths)` pour
  `required`/`risk` ; le repli local (`claude`, `codex`, `deepseek`, mêmes
  valeurs que `train_review.FAMILIES`) reste un paramètre `families` tant que
  la PR85 n'est pas fusionnée ;
- **required, base, tête exacte, classe, commit de canon** : de l'adaptateur
  au gel, à partir de `mechanics.load` (#1616) ou du `Requirement` déjà calculé
  par #1467 (`train_policy.Versioned`) — cx3 a confirmé le 15/10 que le runner
  de #1467 ne voit pas le gel à l'écriture et que le raccord se fait par un
  hook étroit au gel, pas dans `train*` ; `policy_ref` est `rules.commit` (le
  SHA exact de la politique, refusé s'il n'est pas un commit) ;
- **auteurs et correcteurs** : des reçus exacts de #1465
  (`review_provenance.receipt_for_commit`), jamais d'une note ou d'une
  assignation ; l'assigné du lot reste un auteur au minimum. La collecte doit
  fonctionner **avant tout verdict** : `train_review.qualify` attend les avis
  et n'est pas appelable ici. Une preuve manquante est passée en
  `unknown_authors` (attente), jamais ignorée.

## Le hook (branché, inactif)

Point d'accord avec cx3 (#1467, 15/10 10:46) : un hook **étroit** sur
`work.milestone(kind=frozen)`, sans modifier les modules `train*` ni coder de
politique en dur ; cx3 laisse le branchement à ce lot et confirme que #1467 ne
touche ni `work.py` ni `storage/postgres/work.py`. Le raccord est branché :

- `work.milestone(..., on_frozen=…, after_commit=…)` transmet au stockage ;
- `storage.postgres.work.add_milestone` appelle `on_frozen(tx, milestone,
  after_commit)` **dans la transaction du jalon**, juste après l'INSERT du
  `frozen` — le port `PgPort(tx, after_commit=…)` y écrit la marque et les
  courriers atomiquement —, et **ne vide pas** `after_commit` : c'est
  l'appelant qui tient le commit qui la vide après son COMMIT (sur une
  connexion racine, le `with` interne commite et l'appelant vide au retour ;
  dans une transaction déjà ouverte, le `with` imbriqué ne commite pas — vider
  là publierait le fil avant le commit externe, B2 de la revue Codex cx2). Une
  exception du callback annule le jalon avec tout ce qu'il portait ;
  `on_frozen` sur un autre kind
  est refusé.

**Rien n'est activé** : le défaut est `on_frozen=None`, la CLI `work milestone`
ne fournit aucun callback, et un gel ne désigne donc personne. Le raccord est
prouvé par 6 tests base réelle : le jalon et la désignation ne sont visibles
que dans la transaction, le fil reste dans `after_commit` et c'est l'appelant
qui le publie après son commit (appel racine comme appel imbriqué), une
exception annule le jalon, la marque et les courriers, et le callback est
refusé hors d'un `frozen`.

## Vérification

`tests/test_1617_review_designation.py` — 28 tests ciblés, sans base :
maximum des règles par chemin et zéro du léger ; deux familles SQL/sécurité/prod
distinctes et hors auteur ; correcteur devenu auteur écarté (nom et famille) ;
famille d'auteur inconnue = attente (jamais écartée par le seul nom) ; famille
lue sur la session et non sur le nom ; indisponibilité et charge ; attente
explicite notifiée une fois ; provenance d'auteur inconnue = attente, puis
désignation quand la preuve arrive ; capacité revenue = désignation sur le même
gel ; politique Nexlink (deux avis `supabase`, un par défaut) ; même gel rejoué
= un seul courrier ; nouveau gel = nouvelle désignation sans retour à
l'ancienne tête ; gel qui bouge pendant l'écriture = `FreezeMoved` sans rien
écrire, la nouvelle tête n'est désignée que sur un nouvel appel avec ses
entrées ; tête invalide ou non gelée refusée.

`tests/test_1617_review_designation_pg.py` — 21 tests sur base réelle (psql) :
désignation écrite et courriers partis une fois, rejeu sans second courrier ;
un second écrivain (autre connexion) sous le verrou rend la marque de la base
sans remailer ; gel périmé refusé sans rien écrire (ni marque ni message) ;
gel qui bouge pendant l'écriture = `FreezeMoved` sans écriture, la nouvelle
tête se désigne sur un nouvel appel ; attente notifiée une seule fois puis
désignation sur le même gel ; attente sans destinataire refusée ; destinataire
inconnu du registre refusé (un courrier qui ne réveille personne) ; provenance
d'auteur inconnue = attente, désignation quand la preuve arrive ; nouveau gel =
nouvelle désignation, la marque de l'ancien gel reste attachée à sa tête ;
transaction du hook (fil différé, publié par l'appelant après son commit) ;
annulation = ni courrier ni marque. Les 6 tests du raccord du gel : sans
callback le gel ne désigne personne ; le callback voit le jalon dans sa
transaction (invisible d'une autre connexion) et y écrit la désignation, le fil
restant à l'appelant ; une exception du callback annule le jalon, la marque et
les courriers ; un gel imbriqué ne publie aucune trace avant le commit externe
(et rien ne survit à son rollback) ; après le commit externe, l'appelant vide
la liste ; le callback est refusé hors d'un `frozen`.

## Reste avant activation

- accords de port acquis : cx3 (#1467, 15/10 10:46) laisse le branchement du
  hook étroit ici et ne touche ni `work.py` ni `storage/postgres/work.py` ;
  impl07 (#1616) a livré `mechanics.load(repo, commit, project)` (tête
  `ddf1091d`) ; l'adaptateur canon reste à écrire ;
- l'adaptateur d'activation : règles et familles de `mechanics.load`, `required`
  et `risk` de `reviews_for(paths)`, `policy_ref = rules.commit`, pilote à
  notifier (`notify`) — aucune source machine unique du pilote de projet
  n'existe aujourd'hui ; hors hook, le port refuse un destinataire vide ou hors
  du registre sans rien écrire ; **au gel (hook), #1634** : un destinataire vide
  ou hors registre (pilote à notifier ou relecteur) n'annule plus le gel —
  aucun courrier ne part, la marque du gel est une attente (`why` nomme le
  destinataire manquant, réessayable par une reprise hors hook) et une note
  lisible au journal du lot le dit ; les autres échecs du callback annulent
  encore le gel, décision à prendre avec l'adaptateur ;
- la commande de lecture / reprise (`ameesh work reviewers <lot>` ou
  équivalent) pour rattraper un gel resté sans désignation ;
- preuve de livraison sur un **gel réel** : désignation sans agent, tête exacte
  remise, durée et doublons mesurés (le compteur de courriers par gel) ;
- revues : R Claude dev1 (tête `d176acf2`) a trouvé et corrigé B3 (famille
  d'auteur inconnue) → `f6abe89a` ; R Codex cx2 (`f6abe89a`) a rendu `blocked`
  sur B1 (gel bougé) et B2 (commit externe), corrigés ici ; comme dev1 est
  auteur de sa correction, la composition des deux familles sur la nouvelle
  tête est à arbitrer par le pilote ;
- deux avis indépendants hors auteur pour cette porte (sécurité de la chaîne
  d'admission), puis paquet à la coupure.
