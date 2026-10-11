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
| Nouveau travail | `ameesh mail send <agent> "…" --new-lot "RÉF : titre"` |
| Demande d'un humain (L130) | `ameesh work add --title "RÉF : titre" --source "… « phrase exacte »" --priority N`, puis le message `--lot <id>` |
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
      --priority 1|2|3 [--type bug] [--assignee <agent>] \
      --body "Estimation : 2 h"
  ```

  Priorité : 1 haute (bloque l'humain ou un projet), 2 normale, 3 basse.
  Estimation : la durée prévue jusqu'à la PR verte. Le lot L157 (#166) ajoute
  aux lots une durée estimée : une fois livrée, elle se pose par son option de
  `work add` ; d'ici là, une ligne « Estimation : … » dans le corps.
* **Confier** : `ameesh mail send <agent> "…" --lot <id>` sur le lot créé.
  `--new-lot "titre"` confie en un seul geste, et rattache le courrier (qui
  porte la phrase de l'humain) au lot créé, mais sans priorité ni estimation :
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
  `ameesh work add`). Son issue suit au passage suivant d'`ameesh notify`,
  en moins d'une minute.
* Une PR qui livre un lot porte ces deux lignes dans sa **description** :

  ```
  ameesh-work: <numéro du lot>
  Closes #<numéro de l'issue>
  ```

  `ameesh-work` relie la PR au lot. `Closes` ferme l'issue à la fusion, et
  relie aussi la PR au lot par son issue (`ameesh work sync-github`).
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
(`--new-lot`). Un même épisode n'est envoyé qu'une fois.

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

## Livraison et déploiements

Règles du propriétaire (2026-10-10), après qu'une version mineure (1.6.2) est
restée 4 h fusionnée sans être déployée :

1. **Fusionner d'abord.** Une PR à CI verte qui débloque une demande du
   propriétaire se fusionne avant tout nouveau chantier.
2. **Une correction de bug se fusionne et se déploie sans attendre** (règle du
   propriétaire, 2026-10-11) : dès que la CI est verte, avec le déroulé de
   `deploy/mise-a-jour` (vérifier, sauvegarder, installer, migrer, redémarrer
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
5. **Réserver les migrations.** Les numéros de migration se réservent dès le
   début d'un lot, annoncés dans le lot et au journal de conception, pour
   éviter les renumérotations en cascade quand plusieurs lots fusionnent à la
   suite.
6. **Essai à blanc avant toute étiquette de release.** Avant de poser une
   étiquette qui publie (application, installeur, image), le workflow de
   release tourne en essai à blanc sur le SHA exact, sur toutes les plateformes
   cibles ; l'étiquette n'est posée que s'il est vert. On ne déplace jamais une
   étiquette publiée : on pose la suivante.
