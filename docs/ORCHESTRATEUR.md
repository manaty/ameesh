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
`work delegate`) dans les 30 derniers jours. La fiche est la voie durable.

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

## Un agent au repos prend lui-même un lot de son équipe (L165)

Constat du 11/10 : 9 agents Nexlink sur 13 au repos, dont un sans lot depuis
1 h 30, alors que 3 lots Nexlink n'avaient pas d'agent. Désormais, `ameesh
notify` ne laisse plus un lot attendre l'orchestrateur : **un agent
réveillable au repos sans lot depuis 15 min prend lui-même le lot non
assigné le plus prioritaire de son équipe qu'il sait faire**, et
l'orchestrateur de l'équipe en reçoit un courrier (`event`, lié au lot).

* Lots concernés : `intake`, sans assigné, créés il y a plus de 5 min. Un lot
  déjà confié par courrier (`build`), en `waiting_human`, de décision (demande
  de décision en attente, jalon de décision ouvert) n'est jamais pris. Un
  lot dont le début prévu (`ameesh work plan <id> --debut …`) est après
  aujourd'hui attend sa date (exemple : un bilan à deux semaines).
* Réserver un lot à une attribution explicite : capacité exigée `decision` ou
  `production`. Exiger un harnais : capacité `harness:<nom>`.
* Équipe du lot : la sienne, sinon celle de sa fiche (ou de la fiche
  parente), sinon son application (`--app`). Un lot sans équipe connue
  attend l'orchestrateur.
* Ordre : priorité, puis dates prévues (début, fin, livraison), puis
  ancienneté. Un orchestrateur ne se sert pas lui-même.
* Réassigner : `ameesh work assign <id> <agent>`, puis le message `--lot <id>`.
* Réglages : `ameesh notify --take-lot-idle S` (`AMEESH_TAKE_LOT_IDLE`,
  défaut 900, `0` coupe), `--take-lot-max-per-hour N` (défaut 6 pour tout le
  mesh). La file d'amélioration (L119) ne passe qu'ensuite, quand aucun lot du
  projet n'attend.

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
estimation est refusé (`work add`, `mail send --new-lot`) ; un humain reçoit
un avertissement.

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
| Confier un nouveau travail | `ameesh mail send <agent> "…" --new-lot "RÉF : titre" --estimate 90m` |
| Estimer ou ré-estimer un lot existant | `ameesh work plan <id> --estimate 1h30 --estimate-source "revue du découpage"` |
| Voir l'estimation, le réel et l'écart | `ameesh work show <id>`, `ameesh projects` (colonne DURÉE) |
| Écarts passés, pour calibrer | `ameesh work estimates [--app <projet>]` |
| Estimation de référence (table de l'auditeur) | affichée par `ameesh work add` et `ameesh work show` ; `--size s\|m\|l` à la création |

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
  l'assigné, la médiane et le p80 de la durée réelle. `ameesh work add`
  l'affiche à la création (« référence : médiane 25 min · p80 40 min
  (evolution, moyen, 12 lots ; table du … par auditeur) ») et `ameesh work
  show` la rappelle ; ameesh la lit, sans rien recalculer. Déclarer la taille
  du changement (`--size s|m|l` : petit, moyen, gros) et l'assigné affine le
  groupe ; faute de trois lots semblables, ameesh remonte au groupe
  plus large (type et taille, puis type, puis tous les lots). On s'écarte
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
  visible dans `ameesh work sync-branches`.

Le relevé passe toutes les 5 min dans le dossier de travail de l'assigné :
le lot doit être assigné à un agent de l'hôte. Le lot fermé reçoit le
commit de fusion et une note qui dit comment la fusion a été constatée.

À la main, quand le relevé ne peut pas la voir (lot sans assigné, autre
hôte, commit qui ne le désigne pas) :

```
ameesh work merged <id> --sha <commit de fusion> [--note "…"]
```

depuis tout état ouvert, en une fois ; l'acteur est votre identité liée.
L'alerte `stale_lot` rappelle cette commande pour un lot inactif.

Autres fermetures : `ameesh work close <id> --abandoned | --superseded-by
<id>`. Un lot passé `promoted` par erreur : `ameesh work move <id> merged
--correct "raison"` (humains, orchestrateurs et agents de conception
seulement ; tracé au journal).

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
* **`--urgent` seulement quand l'attente bloque** : le destinataire doit agir
  avant de continuer (build cassé sur la branche commune, fusion à arrêter,
  question sans laquelle il tourne à vide). D'un expéditeur habilité
  (`AMEESH_INTERRUPT_SENDERS`), `--urgent` interrompt même un tour en cours :
  à réserver aux vraies urgences.
* **Diffusions** : `agent-mail send all "…"` dépose une annonce lue au
  prochain tour de chacun, sans réveiller personne ; `--urgent` réveille toute
  l'équipe, rarement.
* Un message d'un humain réveille toujours tout de suite.

| Je veux… | Commande |
|---|---|
| confier, demander, répondre | `ameesh mail send <agent> "…" [--lot …]` — regroupé, au plus 90 s |
| que le destinataire agisse sans attendre | `… --urgent` |
| accuser réception | rien ; au besoin `… --ack` |
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
restée 4 h fusionnée sans être déployée :

1. **Fusionner d'abord.** Une PR à CI verte qui débloque une demande du
   propriétaire se fusionne avant tout nouveau chantier.
2. **Une correction de bug se fusionne et se déploie sans attendre** (règle du
   propriétaire, 2026-10-11) : dès que la CI est verte, avec le déroulé de
   `deploy/mise-a-jour` (vérifier, sauvegarder, migrer, installer, redémarrer
   hors tour, contrôler, retour possible). Le propriétaire et les agents
   touchés sont prévenus après coup.
3. **Les autres déploiements se préparent entièrement** (nouvelle fonction,
   version mineure, migration qui change le comportement) : étiquette posée,
   `deploy/mise-a-jour/poste.sh verifier <REF>` et `deploy/mise-a-jour/vm.sh
   verifier <REF>` passés, plan de retour connu (étape `retour`). Ils sont
   proposés au propriétaire dès son retour, en premier point, avec les choix
   possibles : version seule ou `main` entier, migrations, risques. Une
   consigne de veille ne se contente jamais d'interdire « tout déploiement »
   sans le préparer. Les gestes en production d'un projet, les dépenses et les
   secrets restent des décisions du propriétaire.
4. **Redémarrer depuis une unité à part.** `poste.sh redemarrer` ne redémarre
   un exécuteur qu'hors tour. Un agent qui tourne lui-même sous une unité
   d'exécuteur du poste le lance dans une unité systemd distincte :
   `systemd-run --user --unit=ameesh-redemarrer-$(date +%s) bash
   deploy/mise-a-jour/poste.sh redemarrer <REF>`. Un `setsid` ou un `nohup`
   ne suffit pas : le script reste dans le groupe de contrôle de l'unité de
   l'agent, et il est tué quand le script redémarre cette unité, avant
   d'avoir traité les exécuteurs suivants. Une session humaine attachée
   (`ameesh attach`) bloque le redémarrage de son exécuteur jusqu'à sa sortie.
5. **Réserver les migrations** (L122). Le 2026-10-10, quatre lots
   développés en parallèle ont pris les mêmes numéros (0048 à 0050) : trois
   renumérotations en cascade. Un lot qui ajoute une migration réserve son
   numéro **avant d'écrire le fichier** : `ameesh work migration <lot>` note
   dans le lot le prochain numéro libre — au-delà des migrations du paquet,
   de l'arbre courant et de toutes les branches du dépôt (locales et
   distantes, sans fetch : faire `git fetch` avant), et des numéros déjà
   réservés par d'autres lots. La commande est idempotente ; `--number N`
   réserve un numéro précis (refusé s'il est réservé ailleurs), `--release`
   libère. La réservation se lit dans `ameesh work show <lot>` (ligne
   `migration`) et au journal du lot ; la description de la PR l'annonce.
   En confiant un lot qui touche au schéma, l'orchestrateur réserve le
   numéro ou demande à l'agent de le faire en premier. Deux fichiers de même numéro dans
   `src/ameesh/migrations` sont refusés par la suite unitaire
   (`tests/test_l122_numeros_de_migration.py`, sans base).
6. **Essai à blanc avant toute étiquette de release.** Avant de poser une
   étiquette qui publie (application, installeur, image), le workflow de
   release tourne en essai à blanc sur le SHA exact, sur toutes les plateformes
   cibles ; l'étiquette n'est posée que s'il est vert. On ne déplace jamais une
   étiquette publiée : on pose la suivante.
7. **Publier chaque version** (L138). Une fois la PR de version fusionnée,
   `deploy/mise-a-jour/poste.sh publier <sha>` pose l'étiquette annotée
   `v<version>` sur ce commit de `main` (le numéro est celui de son
   `pyproject.toml`), la pousse et crée la release GitHub. Les notes viennent
   de `docs/versions/<version>.md` s'il existe, sinon de la description de la
   PR de ce commit. Seule la plus haute version est marquée « Latest ».
   `poste.sh rattraper` fait de même pour chaque version de l'historique de
   `main` à qui il manque son étiquette ou sa release ; une étiquette déjà
   posée fait foi et n'est pas déplacée. `A_BLANC=1` affiche ce qui serait
   posé, poussé et publié, notes comprises, sans rien toucher. Ces deux étapes
   n'ont besoin que du dépôt et de `gh`, ni du serveur ni de la base.
