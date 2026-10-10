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

## Le courrier « Capacité au repos »

Quand des agents réveillables restent au repos sans lot pendant que du
travail attend, `ameesh notify` envoie à l'orchestrateur un courrier `event`
d'`ameesh` : la liste des agents au repos de son équipe et des lots ouverts
sans agent. Réponse attendue : leur confier ces lots (`--lot`), ou en créer
(`--new-lot`). Un même épisode n'est envoyé qu'une fois.

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
