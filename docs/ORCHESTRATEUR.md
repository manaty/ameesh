# Orchestrateur : la consigne

Un orchestrateur répartit le travail de son équipe entre ses agents. Il ne
décide jamais à la place d'un humain (pas de capacité `approve`, décision
[0010](design/decisions/0010-responsabilite-et-orchestrateurs.md)). Ce
document est sa consigne pour qu'ameesh voie ce qu'il confie : qui travaille
sur quoi, ce qui attend, ce qui est livré.

## Être reconnu comme orchestrateur

ameesh reconnaît un orchestrateur de trois façons : sa fiche Agent du canon
porte `roles: [orchestrateur]`, il est déclaré dans
`AMEESH_ALERT_ORCHESTRATORS`, ou il a confié des lots (`work assign`,
`work delegate`) dans les 30 derniers jours. Cette dernière déduction cesse
dès qu'une fiche de son organisation (son canon) déclare des rôles (L242c) :
la fiche est alors la seule voie durable. Seule compte la fiche de son
propre canon, jamais une fiche homonyme d'un autre.

## Tout travail confié passe par un lot

Un travail confié par courrier sans lot est invisible : `ameesh projects`
montre l'agent « sans lot », les alertes de sous-utilisation se trompent et
la frise reste vide. Règle : **chaque message qui confie du travail porte son
lot**.

| Situation | Commande |
|---|---|
| Nouveau travail | `ameesh mail send <agent> "…" --new-lot "RÉF : titre" --estimate 2h` |
| Demande d'un humain (L130) | `ameesh work add --title "RÉF : titre" --source "… « phrase exacte »" --priority N --estimate 2h`, puis le message `--lot <id>` |
| Lot existant, sans agent | `ameesh mail send <agent> "…" --lot <id\|RÉF>` |
| Prendre un lot sans agent (brief `stale_lot`/`idle_capacity`) | `ameesh work assign <id> <agent> --claim` : refusé si un autre l'a pris entre-temps, jamais de changement de main silencieux (#1026) |
| Reprendre un lot à un autre agent | `ameesh work assign <id> <agent>`, puis le message `--lot <id>` |
| Message sur un lot (relance, question) | `ameesh mail send <agent> "…" --lot <id>` |

* `--lot` accepte le numéro, ou une référence unique : `issue_ref`, fiche du
  plan, branche, premier mot du titre. Commencer les titres par une
  référence courte (« RT-9c : cercle… ») la rend utilisable.
* Un lot qui appartient à un autre agent n'est jamais réassigné par un
  message : ameesh avertit et donne la commande `work assign`.
* Un message à un agent sans aucun lot ouvert déclenche un avertissement
  (sortie d'erreur et fil). Il signale un oubli : rattacher le travail.
* L'attribution passe par la garde habituelle : un agent non réveillable est
  refusé et rien n'est déposé.

## Un agent au repos prend lui-même un lot de ses projets (L165)

Constat du 11/10 : 9 agents Nexlink sur 13 au repos, dont un sans lot depuis
1 h 30, alors que 3 lots Nexlink n'avaient pas d'agent. Désormais, `ameesh
notify` ne laisse plus un lot attendre l'orchestrateur : **un agent
réveillable au repos sans lot depuis 15 min prend lui-même le lot non
assigné le plus prioritaire de ses projets qu'il sait faire**, et
l'orchestrateur de l'équipe en reçoit un courrier (`event`, lié au lot).

* Lots concernés : `intake`, sans assigné, créés il y a plus de 5 min. Un lot
  déjà confié par courrier (`build`), en `waiting_human`, de décision (demande
  de décision en attente, jalon de décision ouvert) n'est jamais pris. Un
  lot dont le début prévu (`ameesh work plan <id> --debut …`) est après
  aujourd'hui attend sa date (exemple : un bilan à deux semaines).
* Réserver un lot à une attribution explicite : capacité exigée `decision` ou
  `production`. Exiger un harnais : capacité `harness:<nom>`.
* Réserver un lot à un **exécutant externe déclaré** (#3447, constat du 25/10 :
  le bureau n'était pas assignable et L165 prenait ses lots) :
  `ameesh work add … --external-executor bureau`, ou
  `ameesh work external-executor <lot> bureau` sur un lot existant. Le lot
  reste **sans assigné** — l'agent `externe` ne se réveille pas (0030) — mais
  il porte son exécutant : L165 ne le prend pas, l'alerte et `ameesh debit` le
  nomment (« attente externe ») au lieu de « sans assigné ». Le geste est
  celui d'un pilote (`corrector`, rôle déclaré) ; `--clear` lève la
  réservation. `work assign` et `work delegate` le refusent tant que la
  réservation tient (un seul exécutant ; la course réservation/délégation se
  tranche sous le verrou de la ligne). Un **sous-lot de préparation** ne
  l'hérite pas : la part d'un agent reste assignable (aucun héritage du champ
  par `parent_lot` ni par la fiche).
* Un lot de relecture après fusion (`review-after-merge:#<lot>`, sur son
  `issue_ref` ou sa `source`, #2377/#2552) n'est pris que par un agent de la
  **famille opposée** à celle de l'auteur au gel ; famille de l'auteur
  inconnue ou non traçable : le lot n'est pas pris au repos et attend une
  attribution explicite.
* Projet du lot, clé de la prise : son application (`--app`), sinon son
  équipe. L'agent le prend s'il figure parmi les projets déclarés de sa fiche
  (`projects`, L243) ; hors du projet primaire, il faut en plus que son
  dossier de travail pour ce projet soit prêt (#811), sinon le lot reste en
  file et une note nominative le dit, sans doublon pour le même agent sur le
  lot — chaque agent qui le tente peut poser la sienne tant que #1538 n'a pas
  ramené la note à une par lot.
* Équipe du lot : la sienne, sinon celle de sa fiche (ou de la fiche
  parente), sinon son application. Elle ne choisit plus le preneur : elle
  désigne les orchestrateurs prévenus et sert au personnel et aux alertes. Un
  lot sans équipe connue attend l'orchestrateur.
* Ordre : priorité, puis dates prévues (début, fin, livraison), puis
  ancienneté. Un orchestrateur ne se sert pas lui-même. Lors d'un appel
  central (`agents` absent), ses candidats passent en dernier et, s'il prend
  un lot faute d'autre preneur, la note d'attribution porte la trace du repli
  (#1739) ; en prise locale (#804, `agents` fourni, même vide), aucun repli :
  son orchestrateur reste exclu (L804).
* En-cours : un agent ne détient pas plus de 2 lots en `build`, tous projets
  confondus (canon §4). La prise refuse le troisième, `ameesh work wip
  --apply` rend le surplus à la file (le lot garde sa branche), et une
  exception pilote est explicite : `ameesh work assign <id> <agent>
  --wip-exception "raison"`.
* Réassigner : `ameesh work assign <id> <agent>`, puis le message `--lot <id>`.
* Réglages : `ameesh notify --take-lot-idle S` (`AMEESH_TAKE_LOT_IDLE`,
  défaut 900, `0` coupe), `--take-lot-max-per-hour N` (défaut 6 pour tout le
  mesh). La file d'amélioration (L119) ne passe qu'ensuite, quand aucun lot du
  projet n'attend.

## Au plus un lot non démarré par agent (#540)

Un agent ne garde qu'**un seul lot non démarré en attente** : un lot ouvert,
assigné, sans début mesuré (aucun tour de son assigné dessus, jamais passé en
`build` ou `qa`). Attribuer un autre lot non démarré à un agent qui en a déjà
un ne le lui donne pas : **le lot va dans la file d'équipe** (sans assigné, en
`intake`), où un agent au repos du projet le prendra (L165 ci-dessus).

* Concerne `ameesh work add --assignee`, `ameesh work assign`, `--lot` ou
  `--new-lot` d'`agent-mail send`, et la prise automatique (L119, L165) : un
  agent choisi au repos qui a reçu un lot non démarré entre-temps ne prend
  rien, le lot reste en file pour le suivant. Le contrôle et l'écriture se font dans la
  même transaction, sous un verrou par agent : deux attributions simultanées
  ne donnent pas deux lots non démarrés au même agent.
* `work add` et `work assign` réussissent, avec l'avertissement « lot #N put
  in the team queue: X already has lot #M not started ». Un lot sans équipe
  connue n'est pris par personne : il attend l'orchestrateur, qui le
  réattribue une fois #M démarré.
* `agent-mail send --lot` ou `--new-lot` : le lot va dans la file et **le
  message n'est pas déposé** (code 2), pour qu'un agent ne travaille pas un
  lot que L165 peut confier à un autre. Renvoyer le message sans `--lot`, ou
  après le début de #M.
* Forcer, pour tout appelant : `--force "<raison>"` (sur `work add`, `work
  assign` et `agent-mail send`) donne le lot malgré tout. La raison et son
  auteur sont écrits au journal du lot ; une raison vide ne force rien (même
  sortie que sans `--force`). L'auteur est l'identité liée de la session :
  un `--actor` ou un `--from` qui en nomme un autre est refusé ; une session
  sans identité dit qui force, et seulement par une identité humaine
  explicite (`--actor human:<id>`) : un nom d'agent y est refusé, un agent ne
  force que depuis sa session liée (bail du runner ou `ameesh mail bind`).
* Hors de la règle : un lot déjà démarré (réassigner un travail en cours reste
  permis), un humain (`human:…`) ou un agent externe (`--externe`), le lot
  d'une demande de décision (`ameesh decide ask --new-lot`, que son auteur
  attend ; seul ce chemin en dispose, ce n'est pas un paramètre de
  `work.add`), la délégation à échéance (L40) et son retour, la reprise sur pause
  budget (L233b, dont le preneur n'a déjà aucun lot).

## Toute demande d'un humain devient un lot (L130)

Constat du 2026-10-11 : depuis le 07/10, le propriétaire a fait 46 demandes ;
27 n'ont eu aucun lot (15 ont pourtant été livrées, sans trace) et une
promesse (« … je le mets juste derrière ») a été oubliée. Ces demandes
arrivent surtout là où rien ne les retient : les conversations avec l'agent
de conception, `ameesh chat` et la session de l'orchestrateur. Règle, pour
l'orchestrateur, l'agent de conception (rôle `conception` de sa fiche au
canon), le chat du propriétaire et tout agent à qui un humain demande
quelque chose : **toute demande d'un humain (évolution, correctif, étude,
geste) est enregistrée sur-le-champ comme lot, et la réponse à l'humain cite
le numéro du lot.**

* **Sur-le-champ** : avant de répondre et avant de commencer, même pour une
  demande de cinq minutes, même pour « je le mets juste derrière ». Si un lot
  ouvert couvre déjà la demande, on y rattache l'échange (`--lot`) et on cite
  ce lot.
* **La phrase exacte en source**, entre guillemets, avec son auteur et son
  canal ; une **priorité** ; une **estimation** de durée :

  ```
  ameesh work add --title "RÉF : titre court" \
      --source "human:<id> (chat|courrier|session) : « <phrase exacte> »" \
      --priority 1|2|3 --estimate 2h [--type bug] [--assignee <agent>]
  ```

  Priorité : 1 haute (bloque l'humain ou un projet), 2 normale, 3 basse.
  Estimation : la durée prévue jusqu'à la fusion (L157, voir « Tout lot porte
  une durée estimée dès sa création » ci-dessous).
* **Confier** : `ameesh mail send <agent> "…" --lot <id>` sur le lot créé.
  `--new-lot "titre" --estimate 2h` confie en un seul geste, et rattache le
  courrier (qui porte la phrase de l'humain) au lot créé, mais sans priorité :
  `work add` reste préférable.
* **Projet** : sans `--app`, le lot prend le projet de son créateur. Un
  `--app` explicite doit nommer un projet connu (#506) ; une faute de frappe
  est refusée avec les noms proches. Le premier lot d'un vrai nouveau projet
  passe `--app <projet> --new-project`, et l'historique du lot l'inscrit.
* **Répondre** en citant le lot : « Enregistré : lot #142, priorité haute,
  estimation 2 h. »
* Une demande refusée ou remise à plus tard s'enregistre aussi : le lot se
  ferme ensuite, avec sa raison (`ameesh work close <id> --abandoned`). Une
  demande ne disparaît jamais sans trace.

Contrôle : `ameesh work unrecorded [--since 24h] [--json]` liste, en lecture
seule, les messages d'humains de la période (leur courrier, leurs réponses aux
décisions, ce que transmet leur chat) qui ne sont rattachés à aucun lot, n'en
citent aucun (`L142`, `lot #142`) et dont aucun lot ne reprend la phrase en
source ([EXPLOITATION.md](EXPLOITATION.md#demandes-dhumains-sans-lot--ameesh-work-unrecorded-l130)).
L'auditeur le passe à chaque passage court. Ce qu'un humain dit directement
dans une session n'est pas en base : seul l'agent qui l'entend peut
l'enregistrer.

## Tout lot porte une durée estimée dès sa création

Demande du propriétaire (2026-10-11, lot L157) : ameesh prévoit la roadmap
complète des projets, donc **tout lot porte une durée estimée dès sa
création**, posée par celui qui le conçoit. Un lot créé par un agent sans
estimation est refusé (`work add`, `work backlog add`, `mail send --new-lot`) ;
un humain reçoit un avertissement.

**Ne jamais répondre « je ne peux pas donner d'heure ».** À une question sur
une durée ou une date de livraison, répondre toujours par une heure : celle de
l'estimation du lot, de la table de référence ou de `ameesh work schedule`, et
à défaut une estimation faite sur-le-champ, avec sa source. Une incertitude se
dit par une fourchette (médiane, p80) et par ce qui la ferait bouger, jamais
par un refus. Un lot sans estimation s'estime à ce moment-là
(`ameesh work plan <id> --estimate …`).

| Situation | Commande |
|---|---|
| Créer un lot | `ameesh work add --title "RÉF : titre" --estimate 2h --estimate-source conception` |
| Enregistrer un défaut non bloquant | `ameesh work backlog add --title "…" --value "…" --score 10 --estimate 20m [--size s\|m\|l]` (sous le seuil, le panier prend la référence s'il n'y a pas d'estimation) |
| Confier un nouveau travail | `ameesh mail send <agent> "…" --new-lot "RÉF : titre" --estimate 90m` |
| Estimer ou ré-estimer un lot existant | `ameesh work plan <id> --estimate 1h30 --estimate-source "revue du découpage"` |
| Voir l'estimation, le réel et l'écart | `ameesh work show <id>`, `ameesh projects` (colonne DURÉE) |
| Écarts passés, pour calibrer | `ameesh work estimates [--app <projet>]` |
| Estimation de référence (table de l'auditeur) | affichée par `ameesh work add`, `ameesh work backlog add` et `ameesh work show` ; `--size s\|m\|l` à la création |

* La durée est du temps **écoulé** (horloge murale), du début du travail à
  la fusion, attentes comprises : `90m`, `2h`, `1h30`, `1,5h`, `2d` (48 h),
  ou des minutes. Au-delà de quelques jours, découper le lot.
* Le **début** est mesuré par ameesh : premier passage du lot en `build`, ou
  premier tour de l'agent assigné sur le lot ; la **fin** est la fusion.
  Rien à déclarer.
* **Partir de l'estimation de référence** (L157b). Constat du propriétaire
  (2026-10-11) : « tu estimes 10 fois trop les tâches d'agents ». À son
  passage quotidien, l'auditeur publie une table de référence tirée des lots
  livrés : par type de lot, taille du changement, hôte et harnais de
  l'assigné, la médiane et le p80 de la durée réelle. `ameesh work add` et
  `ameesh work backlog add` l'affichent à la création (« référence : médiane
  25 min · p80 40 min (evolution, moyen, 12 lots ; table du … par auditeur) »)
  et `ameesh work show` la rappelle ; ameesh la lit, sans rien recalculer.
  Déclarer la taille du changement (`--size s|m|l` : petit, moyen, gros) et
  l'assigné affine le groupe ; faute de trois lots semblables, ameesh remonte
  au groupe plus large (type et taille, puis type, puis tous les lots). Un
  panier ouvert par `work backlog add` sous le seuil prend cette médiane à
  défaut d'estimation explicite (source « référence table #N »). On s'écarte
  de la référence **en le justifiant** dans la source
  (`--estimate-source "référence ×2 : migration de données"`).
* **Estimer par phases.** Une estimation additionne, et sa source détaille,
  les phases jusqu'à la fusion : **agent** (travail de l'agent), **CI**
  (passages de la CI, file d'attente comprise), **relecture** (attente de la
  relecture et de la fusion), **humain** (gestes humains : décision,
  signature, déploiement). Exemple : `--estimate 45m --estimate-source
  "agent 25 min + CI 2×8 min + relecture 5 min"`. Le travail d'un agent est
  court ; ce sont les attentes qui allongent un lot.
* **Le biais de chaque estimateur est suivi.** Le passage de l'auditeur range
  dans la table, pour chaque auteur d'estimation, le ratio réel/estimé
  (médiane, p80) ; hors de ×0,5–×2 sur au moins trois lots, l'alerte
  `estimate_bias` part à l'estimateur et aux orchestrateurs de son équipe
  (courrier `event`). Elle dure jusqu'à la publication suivante qui ne le
  relève plus. `ameesh work estimates` donne aussi les écarts par type de
  lot et par auteur.
* **La consigne d'un lot** demande, dans le message de fin, la durée réelle
  et, si elle s'écarte de l'estimation, la phase qui l'explique (agent, CI,
  relecture, humain).
* **Planned dates at the p80, order at the median** (L157c). `ameesh work
  schedule` shows, for each agent, its queue of open lots with their
  planned dates: lots chained at the median of their reference group (or
  of their estimate), each dated at its p80; `--apply` writes them, keeping
  the dates you set yourself (`--force` to overwrite). `ameesh work add
  --assignee` dates the new lot this way when a reference table is
  published. Declaring the size and the assignee makes the dates closer.
* **The actual time is measured by phase** (L157c). After the merge, the
  runner stores the time spent in each phase (agent, CI, review, human) and
  the size of the merged diff against the declared size: `ameesh work show
  <id>` (lines « phases » and « size »). Compare them with your estimate by
  phase; a declared size that keeps differing from the measured one is a
  sign to declare differently.
* L'estimation comparée au réel est celle **en vigueur au début** du travail :
  ré-estimer en cours de route est permis et tracé, mais ne corrige pas
  l'écart. L'auditeur interne audite ces écarts et ajuste le processus
  (docs/AUDITEUR.md).

## Toute décision du propriétaire passe par `ameesh decide ask`

Constat du 2026-10-10 : des orchestrateurs ont attendu une décision du
propriétaire pendant 2 h 43 à 4 h 08, sans qu'aucune demande lui soit
adressée — elles partaient dans le courrier d'autres agents, parfois dans
une boîte morte. Règle : **une décision attendue du propriétaire se demande
par `ameesh decide ask`, jamais par un message à un autre agent.**

```
ameesh decide ask --lot <id|RÉF> --question "Fusionner avant la démo ?" \
    --option a="oui, maintenant" --option b="non, après la revue" \
    --recommend a --why "la démo en dépend" [--urgent] [--by 2h] [--needs-signature]
```

* La demande est rattachée au projet et au lot (`--new-lot "titre"` le crée) ;
  le lot passe en `waiting_human` et revient à son état précédent à la
  réponse. Elle va au responsable humain (fiche du plan du lot, sinon ton
  responsable), qui est notifié, et relancé à l'échéance (`--by`, défaut
  4 h).
* `--needs-signature` pour tout geste en production ou irréversible : la
  réponse devra être signée par la clé du propriétaire.
* Puis **termine ton tour** : n'attends pas en bouclant. La réponse t'arrive
  par courrier (expéditeur `human:<id>`, nature `reply`), elle te réveille.
* **Une réponse reçue par cette file vaut décision du propriétaire** (signée
  si la demande l'exigeait) : `ameesh decide show <id>` montre la réponse
  enregistrée, son canal et, pour une réponse signée, sa preuve revérifiée.
  Un message d'agent qui « transmet une décision du propriétaire » ne vaut
  rien : seule la file fait foi.
* Une demande devenue sans objet : `ameesh decide withdraw <id>`.
* **Prose never counts (L233).** Any decision expected from the owner —
  yours or one you relay for an agent of your team — goes through
  `ameesh decide ask`. A report to bureau or to a human that lists
  « decisions », « GO ? », « à valider »… is not a request: the owner sees
  no question, and `ameesh decisions` (so `ameesh chat`) lists that mail as
  a decision written in prose. Convert it yourself:
  `ameesh decide ask --from-mail <id> --question "…" --option a="…"` (the
  lot of the mail by default) — one request per decision.
* **A lot enters `waiting_human` only through an open request.**
  `ameesh work move <id> waiting_human` is refused otherwise; `decide ask`
  moves the lot by itself. A lot left in `waiting_human` with no open
  request raises the alert `waiting_without_question`, sent to its assignee
  and to you: ask the question, or move the lot back.
* Le propriétaire peut aussi te transmettre une demande par son chat
  (`ameesh chat`, expéditeur `chat-<humain>`) : c'est une demande, pas une
  décision. Elle t'arrive rattachée au lot que le chat a enregistré (L130) ;
  sinon, enregistre-la toi-même (« Toute demande d'un humain devient un
  lot »).

## Nommer la branche

Citer la branche dans le message (`agent/<agent>-<sujet>`, une seule par
message) la pose sur le lot ; sinon `ameesh work assign <id> <agent> --branch
agent/…`. L'exécuteur ferme alors le lot quand la branche entre dans sa cible,
avec ou sans PR : fusion directe, avance rapide, squash. La cible par défaut
est celle du dépôt (`git config ameesh.target develop` dans le clone si
l'équipe n'intègre pas sur la branche par défaut), ou `--target`.

**Branche `lot/<id>-…`** (L118b). Un lot sans branche déclarée prend pour
branche son unique `lot/<id>` ou `lot/<id>-<sujet>`, locale ou `origin/`.
Elle est posée sur le lot dès que le relevé la voit en avance sur sa cible.
La fusion GitHub de cette branche (« Merge pull request #N from
owner/lot/<id>-… ») ou une fusion locale (« Merge branch 'lot/<id>-…' »)
ferme le lot, même après la suppression de la branche. Le relevé lit alors le
corps de la PR (`gh`).

**Détacher une branche obsolète** (#2102). Quand la PR d'un lot est fermée ou
supplantée et que le lot reste ouvert (vérification restante, autre
livraison), sa branche sort du lot sans changer son état, son titulaire ni
son relecteur : `ameesh work branch <id> --detach --reason "PR #N supplantée
par #M"`. La ligne de journal garde le nom, le dernier commit vu, la cible et
la raison ; Git n'est pas touché. Le radar ne lit plus cette branche, et le
relevé ne l'associe plus implicitement, même si `lot/<id>-…` existe encore.
Un lot sans branche peut nommer celle à ne plus deviner (`ameesh work branch
<id> lot/<id>-… --detach --reason …`). La réassociation est explicite :
`ameesh work branch <id> <branche> [--target T]`. Une PR qui ne livre qu'une partie d'un lot parapluie
(#139) porte dans sa description :

```
ameesh-work-partial: <numéro du lot>
```

Le lot reste ouvert jusqu'à la fusion d'une PR qui ne porte pas cette
ligne (la même ligne dans le message d'une fusion locale le retient aussi,
tant que le relevé n'a pas vu, après avoir vu cette fusion, la branche en
avance avec du travail nouveau, puis ce travail entrer dans la cible ; la
date git de la fusion ne compte pas ; une branche seulement avancée jusqu'à
la cible ne ferme pas le lot).
Si le corps est illisible (`gh` absent ou non connecté), un lot sans
branche déclarée reste ouvert ; une PR plus récente, lisible et complète,
le ferme. Une branche déclarée et prouvée fusionnée ferme le lot comme
avant, et elle seule compte : la fusion d'une autre `lot/<id>-…` ne ferme
pas un lot qui porte une branche. Un relevé lit au plus 20 corps de PR
(un corps déjà lu est gardé, sans nouvel appel) ;
après un échec de `gh`, il n'en lit plus d'autre dans ce dépôt, et la PR en
échec n'est relue qu'après un délai (1 min, doublé à chaque échec, 1 h au
plus). `ameesh work sync-github` suit les mêmes règles et ne retient que
les PR dont la branche est dans le dépôt même, pas dans un fork.

Un lot peut demander plusieurs PR, l'une après l'autre : une PR fusionnée ne
le ferme plus si une autre PR ouverte, ou la branche déclarée du lot (autre
que la sienne), y reste liée — le relevé écrit alors « stage merged » au
journal du lot, qui reste ouvert (#759). La dernière étape lève cette retenue
en se déclarant :

```
ameesh-work: <numéro du lot> final
```

`ameesh-work-partial:` l'emporte : une PR partielle ne ferme jamais, même
avec `final`. Un lot fermé trop tôt par une PR d'étape se corrige
(`ameesh work move <id> build --correct "raison"`), réservé aux humains,
orchestrateurs et agents de conception.

## Fusion sans PR ni branche déclarée

Une équipe qui fusionne en local sur sa branche d'intégration (`--no-ff`,
sans PR) fait fermer ses lots par l'exécuteur en les **désignant dans le
commit de fusion** :

```
git merge --no-ff agent/claude1-veille -m "Merge #93 COMPUTE-IDLE" -m "ameesh-work: 93"
```

* la ligne `ameesh-work: <id>` désigne le lot, toujours ;
* `#<id>` dans le titre du commit de fusion le désigne aussi, **si le
  dépôt l'active** (`git config ameesh.lotRef hash`) — à ne faire que si ces
  numéros sont des lots ameesh : ailleurs « (#45) » est une PR GitHub ;
* la cible est celle du dépôt : poser une fois dans le clone `git config
  ameesh.target origin/develop` (fusions poussées puis récupérées) ou
  `develop` (fusions faites dans ce clone) quand l'équipe n'intègre pas sur
  la branche par défaut. Une cible introuvable est une erreur du relevé,
  visible dans `ameesh work sync-branches`. Sans cible déclarée, le relevé
  suit la cible réelle du projet (#746) : `develop`, `main`, `master` puis
  `release/*` sont essayés, et une fusion citante y ferme le lot, la note
  disant la ref trouvée. Une cible déclarée reste seule à foi : une fusion
  constatée ailleurs n'est que signalée (note au lot, message à
  l'assigné), jamais fermée.

Le relevé passe toutes les 5 min dans le dossier de travail de l'assigné :
le lot doit être assigné à un agent de l'hôte. Le lot fermé reçoit le
commit de fusion et une note qui dit comment la fusion a été constatée.

À la main, quand le relevé ne peut pas la voir (lot sans assigné, autre
hôte), depuis un clone du dépôt du lot :

```
ameesh work merged <id> --sha <commit de fusion> [--repo <clone local>] [--note "…"]
```

depuis tout état ouvert, en une fois ; l'acteur est votre identité liée.
La commande vérifie l'`origin` du clone contre le dépôt de l'issue du lot
(ou le dépôt configuré pour son projet), puis le commit dans sa cible.
Le commit doit désigner le lot par `ameesh-work: <id>`, le titre de fusion
de sa branche, ou `#<id>` dans le titre si `ameesh.lotRef hash` est activé.
Une déclaration partielle est refusée. Un numéro de PR homonyme ne suffit
pas. Depuis #1400, la branche enregistrée du lot est reconnue dans un vrai
commit de fusion (forme GitHub `Merge pull request #N from
<propriétaire>/<branche>` comprise ; le propriétaire doit être celui du
dépôt canonique, la branche homonyme d'un fork ne prouve rien), et le
constat — lot, projet, titre et sujet du commit — est affiché avant toute
écriture ; aucun `--force`. Un refus affiche le projet et le titre du lot ;
aucun état ni jalon n'est écrit. Le SHA abrégé accepté est conservé sous sa
forme complète.
Sans `--repo`, le clone configuré pour ce dépôt est utilisé, sinon le
dossier courant. La lecture reste locale : récupérer la cible au préalable
si nécessaire. Un commit sans rattachement ne ferme pas le lot.
L'alerte `stale_lot` rappelle cette commande pour un lot inactif.

Une PR fusionnée dans un **autre dépôt** que celui du lot (le canon
`manaty/home`, par exemple) se déclare de même, avec `--pr` :

```
ameesh work merged <id> --pr manaty/home#111 [--note "…"]
```

`gh pr view` doit rendre la PR `MERGED` ; sa tête doit être le gel du lot
(ou, sans gel, sa branche). GitHub doit rendre son commit de fusion
(`mergeCommit`), et ce commit doit être entré dans la branche par défaut du
dépôt de la PR : `gh repo view` résout cette branche, puis
`gh api repos/<dépôt>/compare/<défaut>...<merge>` doit rendre `behind` ou
`identical` (un doute refuse). Une PR fusionnée sur une base morte puis
refusionnée dans le défaut passe ; sinon le contenu n'est pas livré
(home#112/#113). `--sha` n'est qu'un contrôle d'égalité avec le commit de
fusion ; absent de la vue GitHub, il refuse.

**PR empilée sur une base déjà fusionnée.** Avant de fusionner une PR dont la
base n'est pas la branche par défaut, vérifier que cette base n'est pas déjà
entrée dans la branche par défaut : sinon la fusion pose le contenu sur une
branche morte et il est perdu (home#112/#113 après la fusion de canon/2452).
La garde est mécanique dans le feu (fusion directe `gh pr merge`), dans la
file de fusion et dans `ameesh work merge-pr` (refus nommé, avec la commande
de correction) ; pour une fusion `gh` directe hors ameesh — bureau —, la
vérification reste manuelle :

```
git -C <clone> merge-base --is-ancestor origin/<base> origin/<défaut> \
  && gh pr edit <N> --repo <owner/repo> --base <défaut>
```

Autres fermetures : `ameesh work close <id> --abandoned | --superseded-by
<id> | --done` (`--done` : lot livré sans fusion, sans branche). Un lot passé
`promoted` par erreur : `ameesh work move <id> merged
--correct "raison"` (humains, orchestrateurs et agents de conception
seulement ; tracé au journal).

## File de fusion par cible (L160e)

Un lot prêt entre dans la file de sa cible, au lieu d'un « feu de fusion »
donné à la main :

```
ameesh work milestone <id> frozen --sha <commit>               le gel (contrôles statiques d'abord, #2542)
ameesh work milestone <id> verdict ok --sha <commit>            le GO de revue, sur ce commit
ameesh work queue add <id>                                      dans la file
```

La tête du gel est un commit **complet** (une abréviation de 12+ hexadécimaux
est résolue dans le dépôt de l'auteur et écrite complète) ; `HEAD` et un nom
de branche sont refusés : chaque lecteur les résoudrait dans son clone, et le
balayage d'un autre arbre a déjà fermé un lot sur ce `HEAD` (#2786, #2712).
Un gel non conforme resté en base vaut doute, jamais une livraison. Un gel
dont le commit est déjà ancêtre de la cible au moment du gel (aucun commit
propre) est refusé : c'est un ancien commit, pas la branche du lot (#2784).
La cible est lue **publiée et rafraîchie** : un clone en retard sur la forge
n'accepte pas un gel déjà livré et n'écrit pas de marque d'absence ; si la
forge est illisible, le gel s'écrit sans cette marque et le balayage doute.

ameesh range la file à chaque passage du radar : priorité, puis les lots
sans conflit, puis ceux dont les conflits sont préparés (`ameesh work
resolve`), puis l'arrivée. Il la rejoue empilée (la cible, plus les lots qui
précèdent, plus celui-ci) et en sort un lot dont un conflit n'est couvert par
aucune résolution, ou dont la branche a bougé après le gel ; son agent est
prévenu. On fusionne le lot de rang 1 (`ameesh work queue show`), puis le
suivant. Un lot en file ne reçoit plus de commit : `ameesh work queue remove
<id>` d'abord.

Un projet qui déclare `land` dans `radar.projects` (`local` ou `pr`, avec
ses portes) est fusionné par ameesh : l'exécuteur prend la tête de file,
fusionne dans son clone avec les résolutions préparées, lance les portes,
pousse sans forcer (ou fusionne la PR une fois la CI verte) et prévient
dans le fil du lot. Un échec sort le lot de la file et réveille son agent ;
l'alerte `merge_blocked` dit qu'une tête de file n'avance plus depuis
30 min. Sans `land` (réglage par défaut), on fusionne soi-même le lot de
rang 1.

Le feu choisit cette voie d'après la **déclaration du projet**, jamais
d'après la configuration locale de l'hôte qui a pris le bail `fire:<lot>`
(#2256) : `land: local` ou `pr` entre en file ; une file partagée connue
prime sur une déclaration locale `off`. La fusion directe exige `land: off`
explicite et l'absence vérifiée de file partagée. Un hôte qui ne déclare pas
le mode d'atterrissage s'abstient (`no-land`) s'il ne trouve pas non plus de
ligne de file pour ce dépôt ; il ne fusionne jamais en direct par défaut.
Une auto-fusion GitHub résiduelle est désarmée avant l'abstention ou l'entrée
en file. Le bail est rendu, et un hôte qui connaît le projet agit au passage
suivant.

## Intégration par paquets §12 : contrôle d'inclusion avant le feu

**Chaque heure**, le pilote compose un petit paquet avec les lots prêts, sans
conflit et aux verdicts complets, sur la tête de la cible (canon
`processus/lots.md` §§11 bis et 12) ; un nouveau paquet attend le précédent.
Aux coupures de 11:00 et 23:00, la release prend la tête de la cible (ou le
dernier commit vert), taguée et déployée en staging : elle ne compose plus de
paquet. Une tête de la cible dont l'arbre est identique à la tête d'une PR de
paquet entièrement verte (même suite complète que le dernier vert de la cible)
compte comme verte, quelle que soit sa propre CI : rouge, annulée, absente ou
en cours (#3633, décision owner #141353 a du 26/10/2026).

Un créneau manqué ne reste pas muet (#2601) : sur l'hôte opérateur, chaque
`ameesh release tick` (minuteur de 5 min) relit la cadence compilée
(`packets.cutoffs`) ; passé la grâce (15 min par défaut), si aucun porteur
`integration/paquet*` ni lot au titre commençant par « Paquet horaire h- »
n’est ouvert (même sans branche) pour le projet et la cible, ni tout juste fusionné,
qu'aucun paquet n'a déjà fusionné pendant le créneau et que la liste calculée
rend au moins un lot prêt, une alerte urgente part, une fois
par créneau, aux destinataires de `packet_watch.projects.<projet>.notify` (le
composeur et le pilote). Le **premier destinataire** est le seul composeur
désigné ; les suivants sont informés. Le repli attend sans réponse au moins
la grâce après l’alerte (15 min minimum), puis vérifie en base qu’aucun lot
de composition n’est ouvert ; le pilote désigne alors un seul remplaçant
(#3406, constat mesh-design du 24/10/2026). Une cible explicite différente
exclut le porteur ; sans cible explicite, le lot occupe conservativement le
projet. `ameesh release packet-watch --dry-run` montre ce qui
partirait.

La composition est **scriptée** (`scripts/paquet-auto.py`, #3807) : le
composeur n'enchaîne plus les outils à la main. Le script part d'un worktree
jetable à la tête exacte d'`origin/<cible>`, y exécute `paquet-candidats.py`
(le code de la cible), compose les candidats dans l'ordre rendu, écarte un
conflit sans le résoudre, passe `freeze-checks`, écrit le manifeste,
`packet record` et le contrôle d'inclusion, puis **dès ce point** (canon §6,
#4304) pousse la branche, ouvre la PR, gèle la tête et demande la CI complète
(`ameesh ci run --full`) ; les tests par unité tournent ensuite, en parallèle
de la CI. Un rouge d'unité retire l'unité et la tête recomposée remplace la
poussée (regel, nouvelle ci-full) ; leur résultat va à la PR, au journal du
porteur et au courrier du pilote. « Verdict non requis » et le feu restent au
pilote. La ci-full arme la fusion automatique et un porteur sans delta propre
n'attend aucun verdict : le paquet peut fusionner au vert **avant la fin des
tests par unité** (canon §11 bis). Le script ne recompose alors rien, un rouge
d'unité part au pilote (corriger en avant ou revert), et `paquet-horaire.py`
ne publie rien sur un porteur déjà fusionné (#4322). Un lot rendu en
**erreur** par `paquet-candidats` n'est jamais composé : il est écarté au
manifeste avec son motif (rc 3 et `errors_by_lot`, #3813). Un run local **non
concluant** (délai, base absente, psql/psycopg indisponible) est consigné à la
PR, au porteur et au courrier, sans retirer d'unité ni arrêter le paquet :
seul un test négatif bloque (canon §3), la ci-full tranche.

Une composition tient un verrou `flock` **par nom de paquet**, sous le `.git`
commun du clone (#4329) : une seconde composition du même paquet — le tic et un
lancement manuel, par exemple — refuse « composition en cours (hôte:pid) »
(code 3) au lieu de supprimer le worktree vivant, et le tic la compte comme en
cours. Le verrou tombe avec le processus : le worktree d'une tentative morte
reste nettoyable comme avant.

```sh
scripts/paquet-auto.py --target main --name h-20261027-01 --carrier 3808
```

`--local` joue le manifeste, `packet record` et les tests par unité, sans
poussée, PR, gel, ci-full ni courrier (composition de contrôle) ; `--replay <réf>` rejoue la table `## Composition` d'un
manifeste gelé et compare l'arbre produit à celui de `<réf>` (témoin de
fidélité, sans poussée ni écriture en base). Avec
`--candidates-json <sortie paquet-candidats --json>`, le rejeu refuse une
unité en erreur ou non prête : h-31 v3 (`b57c0380`, #3506 en erreur) est
refusé, h-31 v4 (`fc59a14e`, 6 unités) passe (arbre `2d15e224…`).
`--exclude LOT` (répétable) retire un lot prêt de la composition et
`--exclude-reason TEXTE` (répétable, apparié par ordre) écrit son vrai motif
aux « Écartés » — B de relecture, regel, rouge local ; sans motif, le
manifeste écrit « exclu par le pilote » (#4201).

La composition **part d'une liste calculée**, jamais des seuls courriers
(#2404) : `scripts/paquet-candidats.py` lit tous les lots `qa` du projet (sans
filtre d'assigné), leur gel effectif confronté à la tête **publiée**, les
verdicts requis sur cette tête et un essai de fusion propre contre la cible. Un
lot prêt absent du paquet doit figurer dans la table « Écartés (ce véhicule) »
du manifeste avec un motif ; sinon il n'est ni composé ni visible. Un lot sous
**hold de pilote actif** n'est pas prêt (`planned_waits`, #3375) : le calcul le
rend `pilot-hold`, motif « hold pilote » au manifeste ; l'autorité de pilote est
lue en strict, donc un registre ou un canon illisible rend la liste
inutilisable (`CandidateError`, rc 1, `--ready-out` retiré) au lieu de libérer
le lot. Le composeur ne le passe qu'avec `--hold-override <lot>
--hold-reason "…"`, la raison étant journalisée sur le lot. Une lecture
impossible **propre à un lot** (gel ou porte illisible) ne retire plus toute la
liste : le calcul sort en `3`, `--ready-out` reste écrit avec une section
`# erreurs` (`!<lot> : <motif>`), et `paquet-inclusion` refuse toute unité en
erreur et consigne les autres comme écartées avec leur motif (#3813). Une
erreur **globale** (base, cible, base de données, inventaire) garde le
comportement ancien : rc 1 et `--ready-out` retiré, y compris un export
précédent, pour que le contrôle s'arrête plutôt que de lire une liste
partielle.

Avant la CI complète et le feu, le contrôle d'inclusion confronte cette liste
calculée, les admissions ou routages du paquet, les lignes du manifeste **et**
le contenu réel de l'arbre :

```sh
scripts/paquet-candidats.py --repo . --ready-out "$TMPDIR/prets-0800.txt"
scripts/paquet-inclusion.py --manifest reviews/2026-10-15-paquet-23h.md \
    --head origin/integration/paquet-23h-20261015 \
    --ready-file "$TMPDIR/prets-0800.txt" \
    --expected-file "$TMPDIR/admissions-23h.txt"
```

Il sort `1` si un lot attendu **ou prêt** manque au manifeste (ni composition
ni « écarté » motivé), si un lot que la liste calculée dit en erreur y est
composé, si un merge de composition n'a pas son trailer `ameesh-work: <lot>`
(#4240 : le composeur corrige le message avant la CI, au lieu de la découvrir
au gel), ou si le contenu d'un lot composé a disparu de la tête (revert non
consigné) — l'ascendance d'une tête ne suffit pas après un revert (#1638).
Sans liste prête, il sort `2` (`--no-ready` est refusé) ; sans liste attendue,
il sort `2` sauf `--no-expected` explicite. Un fichier de lots prêts fourni
mais vide est valide ; un lot en erreur absent du manifeste y est consigné
comme écarté avec son motif (#3813). Sur une recomposition, la liste est
recalculée sur la cible de la recomposition. Un contenu *partiel* ou *vide*
est listé « à lire » (union, adaptation, remise fraîche) ; `--strict` le rend
bloquant.
Détail, format de la liste attendue et validation sur le paquet 11h :
`docs/PAQUET-INCLUSION.md`.

Un lot prêt que le paquet n'emporte pas ne disparaît pas : le manifeste
l'écrit sous « Écartés (ce véhicule) » avec son motif (second avis dû, conflit
à préparer, paquet complet, procédure de release…). Le contrôle d'inclusion
lit cette table comme une explication forte, et refuse une ligne sans motif.
Seul le lot **en tête de ligne** est le sujet d'une exclusion : un id cité
dans le motif (« `#500` dépend de `#600` ») reste une référence faible.

Chaque commit de composition (une unité du paquet) porte un trailer
`ameesh-work: <id>` par lot qu'il porte : il devient déclarable nativement
(`ameesh work merged --sha <commit>`, `sync-branches`) même quand
la remise du lot diffère du gel enregistré (#2157). `scripts/paquet-compose.py`
compose l'unité et écrit ce message, y compris sur conflit (`.git/MERGE_MSG`
prêt, `git commit --no-edit` après résolution) ; aucun paquet déjà validé n'est
réécrit pour ce seul métadonné.
Le corps du lot porteur nomme ses unités avec leur tête, `#<lot> <tête>`, la
ligne qu'imprime `scripts/paquet-compose.py --units --base <cible>` (#2360) :
un `#N` seul n'y déclare aucune unité (#2335). Le manifeste reprend la table
`## Composition` que le script imprime ensuite (colonnes « lot » et
« commit », #3629) : sans colonne « commit », `sync-packets` ne ferme aucune
unité et le dit. Quand ce corps omet la liste,
la lecture des attentes retrouve les unités par ces trailers `ameesh-work:`
de la branche, au-dessus de la cible (#2599) ; l'hôte qui lit retrouve un
clone du registre s'il n'a pas les réglages radar du projet (#2837) ; un `#N`
cité en prose ne supprime toujours pas son alerte.

Après la composition — et après chaque recomposition — le composeur écrit la
liaison en base, depuis le worktree du paquet :

```sh
ameesh work packet record <porteur> --repo <worktree du paquet>
```

La commande relit les commits de composition (`ameesh-work:`, jamais le
manifeste), capture le gel effectif de chaque unité et remplace l'ensemble des
appartenances du porteur (#2443). `ameesh work show <unité>` affiche alors
`package : #<porteur> <branche>` sans lire aucun manifeste ; `ameesh work
sync-merges` ferme une unité quand son commit de composition est entré dans la
cible — y compris sans jalon `frozen`, ce qui couvre le cas #2366 — et refuse
cette preuve si l'unité a été re-gelée depuis la composition. Une composition
non enregistrée reste ouverte : le repli par le gel déclaré est inchangé.

À la fusion du paquet, ses unités se ferment aussi **sans geste manuel** : le
relevé de l'exécuteur lit les manifestes `reviews/**paquet**.md` de la cible
et, pour chaque lot de leur table de composition encore ouvert, examine son
dernier gel par le contenu — ancêtre, patch-id, squash, revert (#1825) — puis
le passe `merged` (`packet_close`, #2426). Une ligne de retrait ne ferme rien ;
un lot re-gelé après la composition reste ouvert. `ameesh work sync-packets
--git-dir D --target T [--dry-run]` fait le même constat à la main, sur les
seules unités des manifestes : c'est le balayage ciblé qui manquait à
`sync-merges` (tous les lots ouverts, plus de 120 s sur ameesh).

## Le lot en cours

`ameesh projects` montre comme lot en cours d'un agent le dernier lot
**ouvert** qu'il cite dans son courrier (`--lot <id|RÉF>`), sinon le lot de
sa session, sinon son lot assigné le plus récent ; jamais un lot fusionné ou
fermé. Demandez aux agents de citer leur lot (`--lot`) dans leurs comptes
rendus, relectures comprises : une référence (`RÉF` du titre) qui désigne un
seul lot ouvert est enregistrée par son numéro, quel que soit l'expéditeur.

## Chaque lot a son issue GitHub

ameesh crée et tient une issue GitHub par lot, dans le dépôt de son projet
(L126). Le titre, l'état (étiquette `ameesh:<état>`), le type, la priorité et
l'assigné suivent le lot. Quand le lot est fusionné, livré ou fermé, ameesh
ferme l'issue avec un commentaire qui dit pourquoi. L'issue d'un lot est son
`issue_ref` (`ameesh work show <id>`), utilisable avec `--lot`.

* Ne jamais créer à la main l'issue d'un lot : créer le lot (`--new-lot`,
  `ameesh work add`, avec `--estimate`). Son issue suit au passage suivant
  d'`ameesh notify`, en moins d'une minute.
* Une PR qui livre un lot porte ces deux lignes dans sa **description** :

  ```
  ameesh-work: <numéro du lot>
  Closes #<numéro de l'issue>
  ```

  `ameesh-work` relie la PR au lot. `Closes` ferme l'issue à la fusion, et
  relie aussi la PR au lot par son issue (`ameesh work sync-github`).
* Après une poussée, ne pas attendre la CI dans le tour (ni
  `gh pr checks --watch`, ni `gh run watch`) : ameesh envoie à l'assigné du
  lot chaque test en échec dès qu'il apparaît (urgent au premier), puis le
  verdict, et retire ce qui n'a pas été lu si une nouvelle poussée arrive
  (L161c). `ameesh ci status <lot>` montre l'état ; voir EXPLOITATION.md,
  « Échecs de CI au courrier de l'agent du lot ».
* Un dépôt public ne reçoit que le titre du lot et sa ligne
  « Résumé public : … », si le corps en a une. Le reste du corps n'est jamais
  publié. Un titre qui contient un chemin local, un nom d'hôte, une adresse,
  un identifiant de compte, un secret ou un terme exclu par l'organisation
  n'a pas d'issue : le refus est journalisé, corriger le titre.
* Une modification faite dans GitHub n'est jamais reprise dans ameesh :
  c'est le lot qu'on change. Garder le marqueur `<!-- ameesh:work=… -->` du
  corps de l'issue, qui la relie au lot.

Les étapes sont aussi commentées sur le ticket : accepté (priorité, cible),
gelé, fusionné, bloqué ou en attente. La projection relit les jalons et les
transitions, même si plusieurs étapes ont eu lieu entre deux passages ; elle
relit les commentaires GitHub pour éviter un second envoi après une panne.
Les commentaires portent `<!-- ameesh:work-stage:v1 … -->`, que la veille
GitHub ignore. Ne pas retirer ce marqueur.
Une annonce de release postée **à la main** sur un ticket (livraison, notes)
est aussi un commentaire du maillage : elle porte le préfixe
`<!-- ameesh: -->` écrit dans le corps, car ce geste se fait souvent hors
d'un tour d'agent et le `gh` généré n'ajoute alors aucun marqueur (#2954,
annonces 1.17.0/#2166 et 1.17.1/#1997). La projection automatique
« Delivered in release X » porte déjà le marqueur `work-stage`.
Ils suivent le ticket miroir sélectionné dans le dépôt du projet, même si
`issue_ref` conserve la référence d'origine dans un autre dépôt ou outil.
La reprise commence sur les tickets ouverts (y compris ceux fermés pendant
la passe) : un ticket déjà fermé avant cette fonctionnalité ne reçoit pas
son historique. Seule une preuve de livraison peut encore y déclencher son
commentaire de release. Les reçus locaux sous
`<état>/github-stage-comments.json` évitent de relire les fils déjà traités ;
un envoi interrompu est réconcilié avec les marqueurs distants. La simulation
ne modifie pas ces reçus. Sans nouvel événement, aucun fil n'est relu.

Le fichier est un cache versionné (`schema: ameesh/github-stage-comments/1`),
clé `dépôt#numéro/lot-<id>` → `{enrolled, seen}`, où `seen` liste `accepted`,
`event-<id>`, `milestone-<id>`, `merged`, `closed` et `delivered-<version>`.
Sans version, il est lu comme v0 puis réécrit avec la version à sa prochaine
écriture ; une version inconnue est laissée intacte (erreur journalisée).
L'hôte écrivain désigné (L126) et l'écriture atomique tiennent lieu de
verrou ; un écrivain concurrent est hors contrat et ses pertes rejouent
depuis les marqueurs. Une entrée illisible est réinitialisée et rejouée. Pour réparer un commentaire supprimé à la main,
retirer sa clé de `seen` (ou l'entrée entière sur un ticket ouvert), puis
relancer la projection : les marqueurs présents ne sont pas redoublés. Sur un
ticket fermé, ne retirer que `seen` pour garder `enrolled` (pas de reprise
d'historique).

Pour préciser ce qui est attendu du demandeur, écrire dans la **note de la
transition** bloquée/en attente une ligne `Public status: …` limitée aux
références et au contrat à confirmer. Les autres lignes restent internes.
Cette ligne passe le contrôle public (secrets, chemins, hôtes, adresses,
termes exclus), même pour un dépôt privé ; un refus est journalisé sans son
texte. Sans ligne publiable, le commentaire annonce l'attente et indique que
l'équipe précisera sur le ticket la décision ou l'information attendue.

« Livré dans la release X, le … » exige le rapport durable de release sous
`<état>/release/reports/` : tag, lot inclus, succès de staging et attestation
d'activation du même commit. Un lot fusionné, promu ou clos avec un jalon
`delivered` ne prouve pas à lui seul une livraison. Sans cette preuve locale,
la projection n'annonce aucune release livrée. Application de
`processus/demandes-github.md` du canon (owner, 20/10/2026, #2844).

## Le courrier qui réveille (L125)

Chaque message qui réveille un agent lui coûte un tour, et chaque tour relit
tout son contexte. Le 2026-10-10, sur un projet de 13 agents, 60 % des tours
duraient moins de 2 min, 87 % des tours de courrier étaient ouverts par un
seul message, et les copies, accusés et diffusions en ouvraient beaucoup.
Depuis L125, un agent au repos n'est réveillé qu'au bout d'une fenêtre de
regroupement (**90 s** par défaut) et son tour emporte tout ce qui est arrivé
entre-temps ([EXPLOITATION.md](EXPLOITATION.md#courrier-regroupé-avant-le-réveil-l125)).
Règles, pour l'orchestrateur comme pour les agents (le pied de chaque tour de
courrier les rappelle) :

* **Pas de copie pour faire relayer : l'orchestrateur lit le fil.** Un agent
  qui écrit à un pair ne met pas l'orchestrateur en copie pour qu'il relaie.
  S'il doit être tenu au courant : `--cc orchestrateur`, une copie **sans
  réveil**, lue à son tour suivant. L'orchestrateur ne relaie pas ce qu'il
  reçoit en copie ; il lit le fil (`ameesh fil show <projet> [<lot>]`).
* **Pas d'accusé de réception seul.** « Reçu, merci » n'appelle pas de tour.
  S'il faut accuser réception : `--ack`, déposé sans réveil. Un message très
  court qui n'est qu'un accusé est reconnu et traité de même ; « ok », « oui »,
  « go », « d'accord » restent des réponses, qui réveillent.
* **Un compte rendu qui n'attend rien** (« rien à faire », « inchangé ») :
  `--info`, déposé sans réveil, lu au tour suivant (#3451). Les avis de prise
  automatique (L165) arrivent ainsi aux pilotes.
* **`--urgent` seulement quand l'attente bloque** : le destinataire doit agir
  avant de continuer (build cassé sur la branche commune, fusion à arrêter,
  question sans laquelle il tourne à vide). De l'owner (`human:*`) ou d'un
  expéditeur habilité (`AMEESH_INTERRUPT_SENDERS`), `--urgent` interrompt même
  un tour en cours : à réserver aux vraies urgences. Depuis #795, la première
  alerte d'échec d'un lot est elle-même un urgent vérifié en base qui
  interrompt le tour de l'agent du lot ; le verdict (vert ou rouge), lui,
  réveille sans couper (#514).
* **Diffusions** : `agent-mail send all "…"` dépose une annonce lue au
  prochain tour de chacun, sans réveiller personne ; `--urgent` réveille toute
  l'équipe, rarement.
* **Un corps qui cite une commande** (backticks, `$(…)`) ne part jamais entre
  guillemets doubles : le shell de l'expéditeur l'exécuterait avant l'envoi
  (#3504, incident du 25/10). Envoyer le corps sur l'entrée standard avec un
  délimiteur entre apostrophes, qui n'interprète rien :
  `agent-mail send <agent> - --lot <id> <<'EOF'` … `EOF`.
* Un message d'un humain réveille toujours tout de suite.

| Je veux… | Commande |
|---|---|
| confier, demander, répondre | `ameesh mail send <agent> "…" [--lot …]` — regroupé, au plus 90 s |
| que le destinataire agisse sans attendre | `… --urgent` |
| accuser réception | rien ; au besoin `… --ack` |
| rendre compte sans rien demander | `… --info` |
| tenir quelqu'un au courant | `… --cc <nom>[,<nom>]` |
| annoncer à toute l'équipe | `ameesh mail send all "…"` |

L'alerte `turn_churn` (`ameesh alerts`) signale un agent qui fait au moins 12
tours de moins de 2 min dans l'heure : la mesure de l'effet de ces règles.

## Le courrier « Capacité au repos »

Quand des agents réveillables restent au repos sans lot pendant que du
travail attend, `ameesh notify` envoie à l'orchestrateur un courrier `event`
d'`ameesh` : la liste des agents au repos de son équipe et des lots ouverts
sans agent. Réponse attendue : leur confier ces lots (`--lot`), ou en créer
(`--new-lot … --estimate`). Un même épisode n'est envoyé qu'une fois.

## Le courrier « Courrier en souffrance »

Un message adressé à un nom absent du registre, ou à un agent arrêté, n'est
lu par personne. `ameesh mail send` le refuse désormais (noms proches
proposés ; `--queue` pour déposer quand même chez un agent arrêté), mais du
courrier peut encore y attendre. Passé 15 min, `ameesh notify` envoie à
l'orchestrateur de l'équipe un courrier `event` d'`ameesh` : le destinataire,
le nombre de messages, leurs expéditeurs, et l'agent qui a repris le travail
s'il est connu. Réponse attendue : re-livrer à l'agent qui porte le travail
(`ameesh mail forward <ancien> <nouveau>`, expéditeur et date d'origine
gardés ; à soi-même si le message nous était destiné), ou faire relancer
l'agent par son responsable (`ameesh resume <agent>`), puis prévenir les
expéditeurs du bon nom.

## Heavy suites: `ameesh offload run` (L159b)

When the runner has `AMEESH_OFFLOAD_HOST` (a lending VM), every mail turn
reminds the agents: full suites, e2e, Postgres suites and builds run with
`ameesh offload run [--profile P] -- <command>`, with the same output and
exit code, and fall back to the workstation if the VM refuses. Targeted
tests stay local. Without that variable, nothing changes. See
[EXPLOITATION.md](EXPLOITATION.md#heavy-suites-on-a-lending-vm--ameesh-offload-l159b).

## Fichiers temporaires : `$TMPDIR`, jamais `/tmp` en dur

Règle de l'orchestrateur et de chacun de ses agents, à rappeler dans tout
travail confié qui produit de gros fichiers (images Docker, clones, caches de
tests) :

* **n'écris jamais en dur dans `/tmp`, utilise `$TMPDIR`** — le dossier de
  l'agent, sur disque, posé par ameesh à chaque tour (`$AMEESH_TMPDIR` est le
  même) ; `docker save -o "$TMPDIR/image.tar"`, `git clone … "$TMPDIR/relecture"`,
  `jest --cacheDirectory "$TMPDIR/jest"` ;
* **rien de plus de 500 Mo dans `/tmp`** : sur un poste, `/tmp` est souvent en
  mémoire (tmpfs) et partagé par tous les agents de l'hôte.

Le 2026-10-11 à 03:38, le `/tmp` d'un poste (12 Go en mémoire, douze agents)
était plein à 83 % : la porte de pression de l'hôte a bloqué tous les nouveaux
tours, et treize agents sont restés au repos. Depuis (L162, #222), une entrée
de `/tmp` apparue pendant un tour est signalée (`tmp_orphan`), mais jamais
supprimée par ameesh : `/tmp` est commun à l'hôte, et rien ne prouve quel tour
l'a créée. L'alerte la suit, taille à jour, avec la commande qui la supprime
quand elle ne sert plus ; et l'alerte d'un tmpfs plein dit les plus grosses
entrées et l'action attendue.
Le texte de chaque tour de courrier rappelle cette règle.

## Review worktrees get no dependencies (#248)

Rule for the orchestrator and every agent it gives a review to. Since the
2026-10-11 19:20 outage (btrfs metadata full: 144 `node_modules` in nexlink
worktrees):

* **a review worktree gets no dependencies**: a review reads the diff; tests
  run in the agent's main worktree, with the branch checked out there. In a
  worktree whose name has `review`, `relecture` or `revue`, the `npm` of the
  turn refuses `npm ci` and `npm install`;
* **elsewhere, `npm ci` links the `node_modules` shared per lockfile**
  (read-only, one copy per host); `ameesh deps local` before
  `npm install <pkg>` or a tool that needs a real directory;
* Rust builds go to `$CARGO_TARGET_DIR` (one per agent), not `target/`.

Details: `docs/SHARED-DEPENDENCIES.md`.

## Ajouter plutôt que modifier (L160a)

Le 10/10, la moitié des blocs en conflit à la fusion étaient deux ajouts au
même endroit d'un fichier commun. Dans les lots que tu confies et que tu
relis :

* **Aucun journal tenu à la main.** L'historique, ce sont git et les PR
  fusionnées ; la description de la PR dit ce qui change et pourquoi. On
  n'ajoute pas d'entrée en tête d'un fichier commun à chaque lot.
* **On ajoute des fichiers plutôt que de modifier un fichier commun** : un
  module, un test, une page ou une décision par sujet.
* **Listes centrales** (`tests/parts/pilote-psql.txt`…) : une entrée par ligne,
  rangée par ordre alphabétique, sa raison en commentaire sur la même ligne ;
  jamais d'ajout systématique en fin ou en tête de liste.

## Livraison et déploiements

Règles du propriétaire (2026-10-10), après qu'une version mineure (1.6.2) est
restée 4 h fusionnée sans être déployée, actualisées selon le
[canon « Processus de lot » §14](https://github.com/manaty/home/blob/c838dd3e94bf/processus/lots.md#14-staging-et-prod-dameesh)
(owner, 2026-10-14 vers 02:45, session bureau Claude
`17076001-2b29-414c-a288-06791a5d9719`), source unique :

1. **Fusionner d'abord.** Une PR à CI verte qui débloque une demande du
   propriétaire se fusionne avant tout nouveau chantier.
2. **Une correction de bug se fusionne et se déploie sans attendre** (règle du
   propriétaire, 2026-10-11) : dès que la CI est verte, avec le déroulé de
   `deploy/mise-a-jour` (vérifier, sauvegarder, migrer, installer, redémarrer
   hors tour, contrôler, retour possible). Le propriétaire et les agents
   touchés sont prévenus après coup.
3. **Le staging d'ameesh se déploie sans go du propriétaire ni procédure de
   release**, y compris sur pc-smichea, pour une nouvelle fonction, une version
   mineure ou une migration additive. Suivre `deploy/mise-a-jour/` : migrations
   additives avant de relancer les exécuteurs, VM d'agents d'abord, pc-smichea
   en dernier et seulement si les VM sont saines (runners redémarrés publiant
   leur vivacité, `ameesh doctor` vert et courrier test livré). Un hôte malsain
   revient à la version précédente (étape `retour`) et le déploiement s'arrête.
   Les hôtes ameesh-agents-1, ameesh-agents-2, manaty-mesh-1 et pc-smichea sont
   le staging ; la prod est une installation utilisée hors du mesh qui construit
   ameesh. **Un déploiement en prod se prépare entièrement** (nouvelle fonction,
   version mineure, migration qui change le comportement) : étiquette posée,
   `deploy/mise-a-jour/poste.sh verifier <REF>` et `deploy/mise-a-jour/vm.sh
   verifier <REF>` passés, plan de retour connu (étape `retour`). Il est
   proposé au propriétaire dès son retour, en premier point, avec les choix
   possibles : version seule ou `main` entier, migrations, risques. Une
   consigne de veille prépare le staging sans lui appliquer cette attente.
   Les gestes en production d'un projet, les dépenses et les
   secrets restent des décisions du propriétaire.
   The watch of an orchestrator is set with `ameesh watch enable --agent
   <orchestrator> --every 30m` (L121): its text, `src/ameesh/consignes/veille.md`,
   carries these rules and the working rules of the canon — integration by
   hourly packages, review before CI and no CI per lot between packages, sober
   proofs (canon « Processus de lot » §§3, 6, 11 bis, 12) — and changes only
   through a reviewed commit.
4. **Redémarrer depuis une unité à part.** `poste.sh redemarrer` arrête chaque
   exécuteur (SIGTERM : le tour en cours draine, borné) avant de le relancer.
   Un agent qui tourne lui-même sous une unité d'exécuteur du poste le lance
   donc dans une unité systemd distincte :
   `systemd-run --user --collect --slice=app.slice --unit=poste-maj-$(date +%s) bash
   deploy/mise-a-jour/poste.sh redemarrer <REF>`. Un `setsid` ou un `nohup`
   ne suffit pas : le script reste dans le groupe de contrôle de l'unité de
   l'agent, et il est tué quand le script redémarre cette unité, avant
   d'avoir traité les exécuteurs suivants. Une session humaine attachée
   (`ameesh attach`) bloque le redémarrage de son exécuteur jusqu'à sa sortie.
   Le script refuse désormais `redemarrer` et `retour` depuis une unité
   `ameesh-*`, avant tout arrêt ; terminer le tour après le lancement détaché,
   puis suivre le journal de l'unité `poste-maj-*`. `controler` refuse aussi
   un notify actif dont le MainPID garde un ancien venv.
5. **Réserver les migrations** (L122). Le 2026-10-10, quatre lots
   développés en parallèle ont pris les mêmes numéros (0048 à 0050) : trois
   renumérotations en cascade. Un lot qui ajoute une migration réserve son
   numéro **avant d'écrire le fichier** : `ameesh work migration <lot>` note
   dans le lot le prochain numéro libre **de son dépôt** (#443) : celui de
   l'issue du lot (`owner/repo#N`, sinon `github.projects` de l'hôte ; sans
   dépôt, refus ; la clé est en minuscules, une seule suite par dépôt), avec
   son dossier et sa largeur (`src/ameesh/migrations` sur 4 chiffres pour
   ameesh, `supabase/migrations` sur 3 pour nexlink ; un autre dépôt se
   déclare dans `radar.projects.<p>` avec `github` et `migrations`). Sans
   numéro, le prochain libre passe au-delà de tout fichier numéroté (`N_…`)
   de l'arbre et de toutes les branches d'un clone local du dépôt (locales et
   distantes, sans fetch : faire `git fetch` avant ; `--repo <clone>`, défaut :
   le clone déclaré à l'hôte, sinon le dépôt courant, dont l'`origin` doit
   être le dépôt du lot sur l'hôte github.com exactement), et
   des numéros déjà réservés pour ce dépôt. `--number N` réserve un numéro
   précis : il est refusé s'il existe déjà **sur la branche cible du lot**
   (#2620 : sa `branch_target`, sinon la cible du projet, sinon la cible
   déclarée du clone — `git config ameesh.target` —, jamais sa branche par
   défaut `origin/HEAD` : sans cible déclarée, la commande refuse (#2660) ;
   `origin/` publiée d'abord ; l'arbre de travail, le paquet installé
   et les branches non fusionnées ne comptent pas — un paquet renumérote une
   migration à sa fusion, #2400) ou s'il est réservé par un autre lot. Chaque
   dépôt a sa suite : la
   même valeur peut être réservée dans ameesh et dans nexlink. L'unicité
   (dépôt, numéro) est garantie en base (index scalaire et index partiel des
   créneaux réservés) : deux réservations simultanées ne prennent jamais le
   même numéro ; la perdante réessaie après une attente plein jitter (entre 0
   et un plafond croissant), bornée au budget restant, et si le budget
   s'épuise la commande rend « reservation contention, retry » (jamais
   l'erreur brute de l'index). Seule la violation d'unicité de ces index
   déclenche un réessai ; une autre erreur qui le nomme remonte. La commande est
   idempotente ; `--number N` réserve un numéro précis, refusé s'il existe
   déjà sur la branche cible du lot (`branch_target`, sinon la cible du
   projet, sinon la cible déclarée `ameesh.target`, `origin/` publiée
   d'abord ; sans cible déclarée, refus — jamais `origin/HEAD`, #2660 ; les
   branches non fusionnées ne comptent pas, #2620) ou s'il est réservé
   ailleurs. Un lot qui
   ajoute plusieurs migrations en réserve plusieurs (#2481) : chaque
   `--number` s'ajoute à sa liste `migrations`, le premier numéro restant la
   réservation principale lue par les clients d'avant #2481 ; `--release
   --number N` ne libère que ce numéro, `--release` les libère tous.
   `--skip-branch BRANCHE` (répétable, #1603) ajoute une branche au scan
   ignoré, en plus de celle du lot : à la composition d'un paquet, la branche
   d'intégration porte déjà les fichiers renumérotés et ferait choisir un
   numéro au-delà pour l'allocation du prochain libre ; l'option ne dispense
   pas de la réservation (un
   numéro retenu par un autre lot reste refusé), et une branche de tronc
   (`main`, `master`, `develop`, la branche par défaut d'`origin`) ne s'ignore
   jamais. Une réservation à numéro exact lit la cible seule : `--skip-branch`
   n'y change rien. Un lot dont l'`issue_ref`
   désigne un autre dépôt se corrige avant de réserver :
   `ameesh work issue-ref <lot> owner/repo#N` (ou `--none` pour effacer),
   réservé aux correcteurs comme `work set-app` et journalisé ; refusé tant
   qu'une réservation est tenue dans l'ancien dépôt (`--release` d'abord). La réservation
   se lit dans `ameesh work show <lot>`
   (ligne `migration`, plusieurs numéros sur la même ligne s'il y en a
   plusieurs) et au journal du lot ; la description de la PR l'annonce.
   En confiant un lot qui touche au schéma, l'orchestrateur réserve le
   numéro ou demande à l'agent de le faire en premier. Deux fichiers de même numéro dans
   `src/ameesh/migrations` sont refusés par la suite unitaire
   (`tests/test_l122_numeros_de_migration.py`, sans base). À la composition
   d'un paquet par le train (#2400), chaque migration ajoutée par un lot est
   renumérotée au maximum de la cible + 1, puis + 2, … dans l'ordre des
   fichiers ; un lot à plusieurs migrations les fait donc renuméroter toutes,
   jamais une migration historique : le lot qui en modifie une est écarté avec
   un motif. Les fichiers compagnons
   du même préfixe (rollback, test, banc) sont renommés avec elle, les
   références par nom de fichier et les numéros en commentaire suivent ; les
   littéraux de code ne changent pas. Un transport qualifié qui porterait une
   migration ajoutée ou modifiée est refusé et le lot écarté : sa qualification
   fige l'arbre et le delta, la renumérotation doit donc précéder la
   qualification. Les réservations L122 sont déplacées (ou ajoutées) avant
   la publication (journal « before: … »), le plan porte une correspondance
   (`reserved`/`landed`) par numéro et le commit final une ligne
   `ameesh-migration: <lot> <ancien> <nouveau>` par numéro. Après promotion
   confirmée, le premier numéro atterri est enregistré (`migration_landed`,
   la correspondance complète reste dans le plan et le journal) et toutes les
   réservations du lot sont libérées. Le train reste inactif : ce raccord ne
   l'active pas.
6. **Geler le périmètre d'une release à son ouverture** (L750/L756, #561/#566).
   Quand le lot de release est ouvert, son périmètre se fige à un commit
   **nommé** de sa cible : `ameesh release open <lot> --base <commit>`. Les
   fusions de `main` postérieures au gel vont à la release suivante ; seuls les
   correctifs d'un bloquant de cette release sont cherry-pickés sur
   `release/x.y`, chacun avec sa propre exécution impactée. La suite **complète**
   tourne une fois sur la tête gelée — une PR de version seule ne lance rien, et
   une PR peut n'avoir suivi que le plan impacté. Avant de fusionner la PR de
   version, `ameesh release check <lot>` refuse une branche dont la base a
   bougé après le gel (le merge-base avec la cible n'est plus le périmètre) et
   l'absence d'une exécution de suite complète (poussée ou lancement manuel)
   verte sur la **tête fraîche** exacte : un commit postérieur au gel (correctif
   cherry-pické) est certifié lui-même, jamais par la CI de l'ancien gel. La
   suite complète se **déclare** par ses fichiers de workflow dans
   `radar.projects.<p>.full_suite` (une identité — le fichier —, jamais un nom
   d'affichage libre) ; chacun doit avoir tourné sur le commit exact, vert
   **jobs compris**. Une entrée peut nommer ses jobs requis
   (`{"path": …, "jobs": ["plan", "psycopg *"], "tolerate": ["bilan des
   impactés"]}`) : « nom * » couvre une famille de matrice (sinon égalité
   stricte), un job requis doit exister et être vert, les autres doivent être
   verts ou sautés (un job sauté par conception, `if:` sur l'événement ou les
   chemins, ne bloque pas) et un job `tolerate` (`continue-on-error`) ne
   certifie ni ne bloque. Une entrée réduite au chemin exige **tous** les jobs ;
   un job illisible ne certifie rien, et le dispatch d'un workflow non déclaré
   (docs seul) ne compte pas. Un écart
   délibéré se justifie par `ameesh release reason <lot> --text "…"` ; le
   périmètre et la raison vivent au journal du lot. La file de fusion applique
   la même porte au lot de classe release qu'elle traverse
   (`merge_queue.readiness`).
7. **Essai à blanc avant toute étiquette de release.** Avant de poser une
   étiquette qui publie (application, installeur, image), le workflow de
   release tourne en essai à blanc sur le SHA exact, sur toutes les plateformes
   cibles ; l'étiquette n'est posée que s'il est vert. On ne déplace jamais une
   étiquette publiée : on pose la suivante.
8. **Publier chaque version** (L138). Une fois la PR de version fusionnée,
   `deploy/mise-a-jour/poste.sh publier <sha>` pose l'étiquette annotée
   `v<version>` sur ce commit de `main` (le numéro est celui de son
   `pyproject.toml`), la pousse et crée la release GitHub, avec en actifs la
   roue ameesh du commit, `runtime-pins.txt`, `runtime-build-pins.txt` et
   `SHA256SUMS`, construits avant l'étiquette (#4786). Les notes viennent
   de `docs/versions/<version>.md` tel qu'il est dans `main` (une note se
   relit et se fusionne avant de publier), sinon de la description de la PR
   qui a fusionné ce commit (vérifié auprès de GitHub) ; une description
   illisible arrête la publication. Seule la plus haute version est marquée
   « Latest ». `poste.sh rattraper` fait de même pour chaque version de
   l'historique de `main` à qui il manque son étiquette ou sa release ; une
   étiquette déjà posée sur le distant fait foi et n'est pas déplacée, si son
   commit porte bien cette version (sinon tout s'arrête). Une étiquette posée
   ici mais pas poussée est poussée à la relance. `A_BLANC=1` affiche ce qui
   serait posé, poussé et publié, notes comprises, sans rien écrire, pas même
   un `git fetch`. Ces deux étapes n'ont besoin que du dépôt et de `gh`, ni
   du serveur ni de la base.
