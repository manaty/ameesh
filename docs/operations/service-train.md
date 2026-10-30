# Le service de train automatique (#2446)

Le moteur du train (#1467) existait sur `main` mais aucun code de production
ne construisait ses passages : `Runner.train_passages` restait vide et les
paquets `h-*` étaient composés à la main. Ce service assemble les passages
depuis la configuration de l'hôte et les démarre. Il est **éteint par défaut**
et ne s'active que par une configuration explicite.

## Configuration

La clé `train` du fichier de configuration de l'hôte (le même
`~/.config/ameesh/config.json`, ou un profil d'exécuteur) décrit le service :

```json
{
  "train": {
    "schema": "ameesh-train-host/1",
    "host": "ameesh-agents-2",
    "mode": "shadow",
    "projects": {
      "ameesh": {
        "repo": "/home/ameesh/work/ameesh-dev5",
        "target": "main",
        "github": "manaty/ameesh",
        "canon": "/home/ameesh/canon",
        "canon_commit": "21d6653922bac554d2d6cd0570e7f6497b215c37",
        "ci_contract": "ameesh",
        "provenance": "review_provenance",
        "reservation": "packet_reservation",
        "deadline_minutes": 120,
        "candidate": true,
        "initial_since": "2026-10-18T11:00:00+02:00"
      }
    }
  }
}
```

Chaque champ est obligatoire ; une clé inconnue est refusée. Les valeurs
`ci_contract`, `provenance` et `reservation` nomment un adaptateur installé
(`ameesh`, `review_provenance`, `packet_reservation`) ; un autre nom est un
refus nommé, jamais un repli. `deadline_minutes` est l'échéance absolue de la
réservation du paquet, comptée à partir de l'instant de la publication ; il
n'existe aucune durée par défaut.

`candidate` active l'inventaire des lots `qa` et leur inscription explicite
dans la file. `initial_since` est alors l'instant ISO-8601 (avec fuseau) à
partir duquel le train est l'unique écrivain de la file : un gel né après cet
instant et sans reçu d'allocation/retrait est une absence initiale qualifiée ;
un gel plus ancien reste `queue-history-unknown` et attend un humain.
Sans `candidate`, le train ne consomme que les lots déjà en file.

Le service ne s'assemble que sur l'hôte désigné par `train.host` : les autres
hôtes lisent la configuration et n'assemblent rien (une ligne de journal le
dit). Un seul hôte exécute le service grâce au bail `train-service` (table
`merge_locks`, TTL 15 min, renouvelé, rendu à l'arrêt) : si deux agents ou deux
hôtes le tentent, un seul obtient le bail ; les autres attendent et réessaient.
Le propriétaire du bail porte un identifiant d'incarnation unique : deux
processus configurés avec le même identifiant d'exécuteur ne peuvent pas
reprendre le bail l'un de l'autre (le SQL autorise le même propriétaire à
prendre un nouvel epoch). Après un arrêt brutal, le successeur attend donc
l'expiration du TTL ; un arrêt propre le libère tout de suite.

Les réservations de cible (`<projet>:<cible>`, propriétaire `train:<exécuteur>`)
ne suivent pas le bail de service : un exécuteur arrêté laisse les siennes
posées. Le nouveau porteur du bail les adopte (#3142) au lieu d'attendre leur
TTL de 30 min : dans une même transaction, son bail de service vivant est
vérifié, la réservation passe à son propriétaire `train:` à l'epoch suivant, et
le ticket du plan en vol est réécrit. L'ancien epoch est ensuite refusé partout
(sauvegarde du train, renouvellement, publication, revendication de
promotion, libération). Le plan reprend où il était : une publication
incertaine est republiée à l'identique (idempotente), une promotion incertaine
passe par la réconciliation. Une réservation ordinaire (fusion d'un lot seul)
n'est jamais adoptée, ni un ticket dont l'epoch ne correspond plus à la ligne.

## Modes

### `shadow` — le plan calculé, sans effet

À chaque créneau du calendrier compilé (`packets.cutoffs` du canon), le
service :

1. ouvre le dépôt et lit la cible fraîche ;
2. lit l'inventaire `qa` et prépare chaque candidat (gel exact, avis
   historiques, classe et nombre d'avis compilés) ;
3. publie le plan dans
   `$AMEESH_STATE/trains/shadow/<projet>-<cible>-<empreinte>.json` ;
4. compare ce plan au manifeste du composeur humain quand le paquet de la même
   heure a atterri : lots manquants (avec le motif que le train avait), lots en
   trop, paquet et base qui ont servi. Un lot déjà composé dans un paquet
   `integration/**` encore en vol au créneau (paquet fusionné après la coupure,
   ou branche encore ouverte, lue en lecture seule) n'est pas un désaccord : il
   est exclu des « en trop » et nommé « porté par <paquet> en vol » dans
   `carried` et `explanations`. La composition au créneau est **prouvée** au
   dernier commit first-parent de la branche antérieur à la coupure, avec le
   manifeste exact `reviews/integration-<paquet>.md` : le manifeste final d'une
   branche enrichie après la coupure ne compte pas, et aucun manifeste d'un
   autre paquet n'est repris (pas de repli). Sans preuve, le lot reste dans
   `extra`, expliqué « porteur inconnu » ; une branche ouverte dont la dernière
   composition avant la coupure dépasse six heures est tenue pour abandonnée,
   jamais porteuse.

L'inventaire de l'ombre applique la **même décision d'inscription** que le mode
actif (`Enrollment.decide` : préparation, avis, puis frontière initiale et
reçus de retrait), sans écrire : un gel retiré, d'historique inconnu ou une ligne de file sans reçu d'allocation
n'est jamais présenté comme un membre que le train inscrirait.

Une cible autre que `main` reçoit la **synchro mécanique de main** (#2727) :
quand le paquet a des membres et que `main` porte des commits absents de la
cible, le plan porte `sync: {ref: "main", sha: …}` et le paquet réel fusionne
ce commit en tête, sans avis ni lot build. La comparaison expose le fait dans
`sync` (`plan`, `manual`, `lots`) et retire des lots manquants celui d'une
fusion manuelle **prouvée pure** : deux parents (côté paquet, `main`), un seul
trailer `ameesh-work`, arbre identique à la fusion propre des parents. Une
fusion qui ajoute autre chose, résout à la main ou porte plusieurs trailers
laisse son lot manquant (`sync.refused`) : une synchro non prouvée n'est pas un
écart de composition.

Rien n'est réservé, poussé, dispatché ni fusionné : aucun `merge_locks` de
cible, aucune branche `integration/**`, aucune CI. C'est le mode qui doit
tourner sur au moins deux paquets horaires réels avant la bascule.

Lecture :

```sh
jq '.plans[-2:]' "$AMEESH_STATE"/trains/shadow/*.json
jq '.comparisons[] | {packet: .packet.stamp, missing, extra, carried, explanations}' \
  "$AMEESH_STATE"/trains/shadow/*.json
```

Les comparaisons sont conservées (24 plans, 48 comparaisons). Un plan dont le
paquet n'a pas encore atterri n'apparaît pas dans `comparisons`.

### `active` — la bascule

Le passage réel du moteur #1467 : il compose la tête du paquet, publie la
branche immuable `integration/train-*`, attend l'unique CI complète du paquet
et fusionne au vert. La forme de cette preuve est celle du contrat CI du
projet (voir « Contrat CI ») : pour ameesh, le run `push` de `paquet.yml`, dont
le job `suite` appelle `tests.yml` (jobs appelés dans le même run, préfixés
par le job appelant) ; pour Nexlink, les runs `workflow_dispatch` des seules
suites que le change set intégré rend dues. Un push ameesh couvert par une PR
ouverte laisse `suite` sauté : ce n'est pas une preuve de paquet, le train
refuse. Le composeur humain devient le repli : en cas de rouge, le train
s'arrête, alerte et n'enchaîne pas ; un humain reprend la main.

Avant les membres, le train fusionne le commit `main` que la cible ne porte
pas encore (membre mécanique #2727, sans avis) : la tête du paquet porte
`ameesh-sync: main <sha>`, cite `main` parmi ses parents et reste reproductible
depuis le plan. Un conflit refuse le paquet en nommant `main`, la base et les
fichiers ; un fichier **généré** du projet fait exception : il est régénéré sur
l'arbre fusionné comme à l'atterrissage, et un échec de régénération refuse le
paquet. Une réécriture de `main` qui retire le commit planifié refuse aussi la
publication. Une cible `main` n'a évidemment pas cette synchro.

La bascule est un geste de pilote : changer `mode` en `active`, vérifier le
commit canon audité et l'échéance, puis redémarrer l'exécuteur. Aucun code ne
bascule tout seul, et le service ne redémarre pas l'exécuteur à la place d'un
humain.

## Refus nommés

Le service ne démarre pas (le reste de l'exécuteur continue de servir les
agents) et journalise le premier refus :

- clé `train` absente : service inactif, comportement historique ;
- schéma, mode, clé, champ ou adaptateur inconnu/manquant ;
- dépôt ou canon introuvable, commit canon non complet, `mecanique.yaml` ou
  `federation.yaml` absent/mal formé au commit exact ;
- cible `release/*` ou `integration/*` ;
- `deadline_minutes` absente, nulle ou non finie ;
- `candidate: true` sans `initial_since` (ou l'inverse).

### Canon épinglé ≠ canon de la porte (#2914)

À chaque passage, le train compare `canon_commit` au commit de canon que lisent
la porte de fusion et la désignation sur l'hôte (`canon_state.last_good_commit`,
`canon_sync.audited_commit`), choisi comme la désignation le choisit : premier
canon configuré dont la politique du projet se charge
(`review_designation_activation.policy_sources`) ; sinon les deux ne demandent
pas le même nombre d'avis pour un même lot. Un écart, un canon de la porte
illisible, plusieurs canons applicables à des commits différents (le choix
dépendrait alors des chemins du lot), ou, à commit égal, une autre lecture
de la porte différente du commit épinglé (`train_canon.gate_rules_gap`) :

- classes et barèmes (`lot_class.of_lot`) : chaque canon configuré qui déclare
  le projet, lu à sa **référence configurée** (pas au commit audité), doit
  être au commit épinglé ;
- plancher léger et drapeaux de famille : le plus strict de **tous** les
  canons configurés, applicables au projet ou non
  (`lot_class._light_reviews`, `_review_flags`) ;
- familles de modèle (`review_lot.compiled_families`, premier canon lisible).

Dans tous ces cas :

- `shadow` : le plan porte la lecture (`plan.canon` : `pin`, `gate`, `state`) ;
  le journal la nomme et l'orchestrateur du projet reçoit un courrier, une fois
  par lecture distincte et par processus ;
- `active` : refus nommé de composer un nouveau paquet (`canon-drift`), même
  courrier ; un paquet déjà en vol va au bout (CI, promotion ou vidange), un
  train en pause reste en pause.

Le service fait suivre automatiquement l'épingle à la porte auditée (#3483),
avant chaque passage et sur les hôtes en attente du rôle ou du bail. Chaque
hôte vérifie localement l'avance : la porte doit descendre de l'épingle et le
diff ne doit toucher aucun fichier de la liste fermée, versionnée dans
`train_assembly.REQUALIFY_GUARD` : `processus/mecanique.yaml`, `federation.yaml`.
Les renommages et suppressions sont vérifiés également. Sinon, refus explicite
et requalification relue requise ; une porte illisible ne fournit aucun repli.

L'écriture couvre `config.json` et tous les profils hérités qui déclarent une
épingle ; les autres héritent. Le verrou `.train-canon.lock` dans le dossier
de la configuration hôte sérialise les exécuteurs et la commande manuelle. Le journal durable
`train-canon.jsonl` au même endroit conserve hôte, ancien et nouveau SHA,
chemins du diff vérifié et liste de garde. Une erreur de journalisation
restaure les fichiers. Le service recharge ensuite l'épingle effective (#3366).
Le suivi ne modifie ni les références ni les règles de la porte. Un refus du suivi
est signalé par projet, une fois par motif jusqu'au rétablissement. Le passage
recharge toujours son épingle configurée (#3366), même si le suivi automatique
échoue : une épingle intermédiaire qualifiée par le pilote reste applicable.
Les autres projets poursuivent leur suivi.
La garde de dérive refuse toute nouvelle composition non qualifiée ; les
paquets déjà en vol peuvent atteindre leur état terminal.

La commande pilote `python -m ameesh.train_cli --project <p> requalify --lot
<lot>` reste disponible avec sa trace sur le lot ; `--dry-run` montre le diff.
Elle exige aussi que la porte descende de l’épingle et que le diff laisse les
fichiers de garde intacts ; un retour arrière exige une requalification relue.
Un changement de `mode`, de `canon`, de dépôt ou de cible exige toujours un
redémarrage ; seul l'épinglage est relu.

Si la plage modifie les fichiers de garde, utiliser `requalify --reviewed
--lot <lot> --review milestone:<id>` (option `--review` répétable). Des avis
sur la porte finale qualifient toute la plage. Quand les PR successives ont
des auteurs de plusieurs familles, fournir les avis de chaque PR qui modifie
un fichier de garde, ainsi qu'un avis actuel du pilote sur la porte finale.
Les références intermédiaires activent la qualification par premier parent :
chaque PR conserve ses propres auteurs et avis indépendants, sans cumuler les
familles des auteurs des autres PR. Une fusion propre peut reprendre les
avis sur sa tête de PR exacte ; une fusion modifiée ou conflictuelle exige
des avis sur le commit de fusion. L'épingle doit appartenir à cette chaîne
de premiers parents. Les oppositions sur les têtes ou leurs fusions restent
bloquantes ; les règles de la plage restent un plancher. Toutes les preuves
sont vérifiées avant la première écriture et gardées dans la trace de
requalification. `--dry-run` vérifie ces mêmes conditions sans déplacer
l'épingle.

Quand le commit est le même et que seules les autres lectures des règles
diffèrent (`gate_rules_gap`), il n'y a aucun commit à requalifier : aligner les
sources de la porte sur le canon épinglé (références configurées,
plancher/drapeaux, familles compilées), ou qualifier un nouveau canon si
l'épingle doit changer. Le plan d'ombre suit la lecture à chaque passage :
`plan.canon` est réécrit si l'écart apparaît (ou se résout) après
l'enregistrement du plan du créneau.

## Contrat CI

`train_contracts.ameesh()` épingle les blobs de `.github/workflows/paquet.yml`
(le run appelant), `.github/workflows/tests.yml` (le workflow appelé), du
classifieur de l'agrégat (`ci-agregat.py`, #3251) et des planificateurs
(`test-plan.py`, `test-parts.py`, `ci-flow.py`, `map.py`). La
preuve est le run `push` de `paquet.yml` à la tête exacte : il doit référencer
`tests.yml` au même SHA et porter les jobs appelés, préfixés par le job
appelant (`suite complète du paquet / plan`, `… / psycopg 4/8`, `… / tests`) :
la matrice complète 8 psycopg + 16 psql, le job **court** `plateforme macOS`
(L63/L66, #3198) et l'agrégat `tests`. La suite complète sous macOS n'est plus
dans ce contrat : elle vit dans `.github/workflows/macos.yml` (nocturne sur
`main`, `ci-macos` sur une PR) et ne retient plus le paquet. Une PR ouverte
laisse `suite` sauté : les jobs appelés sont absents et le train ne s'en sert
jamais. Une modification d'un de ces fichiers exige un nouveau contrat audité :
le témoin `tests/test_2446_train_contract.py` compare les blobs au commit.

#3098 : les blobs de référence ne sont pas lus dans le module installé, qui
reste l'instantané de la release, mais dans `src/ameesh/train_contracts.py` au
**commit de base** de la cible — code relu et atterri. Le module installé
fournit les chemins et un **plancher** de jobs. Un paquet qui change une
source est donc refusé, et une fois ce paquet atterri par la voie « déblocage
CI » (§11 bis) avec son ré-épinglage, la coupure suivante repart immédiatement,
sans attendre la release et le déploiement suivants. **Toutes** les sources
déclarées par la base sont vérifiées, y compris celles que le module installé ne
connaît pas encore : la tête ne peut pas les déplacer. Une base dont les épingles
ne correspondent pas à son propre arbre, qui omet une source auditée, ou dont le
commit est absent du clone, est refusée par un message nommé. Témoins :
`tests/test_3098_train_contract_pins.py`.

#3108 : les jobs déclarés par la base s'ajoutent à ceux du module installé.
Un job ajouté par un workflow ré-épinglé est ainsi exigé dès la coupure
suivante, sans attendre le déploiement ; un job retiré reste exigé par le
plancher installé, donc une baisse du contrat n'est pas exploitable avant la
release. L'expression `jobs=` de la base est lue statiquement — constantes,
tuples, étoilement, compréhensions `range` et formatage `%`, jamais exécutée ;
toute autre forme est refusée nommément. Témoins :
`tests/test_3108_train_contract_jobs.py`.

### Nexlink — les parts dues (#2503)

Le paquet Nexlink n'est pas un run unique : chaque suite de lot est un
`workflow_dispatch` qui publie `lot/<suite>`, et les parts dues sont calculées
par `scripts/ci/lot-plan.mjs` sur le change set intégré. Le contrat ne fige
donc **pas** les 23 suites : il épingle seulement le sélecteur
(`scripts/ci/lot-plan.mjs`) et les workflows partagés de plan et de statut
(`.github/workflows/lot-plan.yml`, `.github/workflows/lot-status.yml`), au
commit `NEXLINK_AUDIT_COMMIT` (`9103c167`, develop du 18/10 19:09 Paris). Le
sélecteur de la cible est vérifié au commit de base (une modification exige un
nouveau contrat) ; `ci_parts.due` (bac à sable `bwrap`, aucun repli) rend les
contextes `lot/<suite>` dus. Le même sélecteur audité tourne sur **deux
arbres** : celui de la base (le plan déjà publié par la cible, qu'une tête ne
peut pas réduire) et celui de la tête (les suites que le paquet ajoute ou
élargit). Le sélecteur de l'arbre de la tête doit être le même blob que celui
de la base : un sélecteur modifié est refusé, jamais exécuté. L'union des deux
plans est exigée ; chaque fichier doit déclarer le même `context` à la tête
dispatchée, puis il est dispatché avec `expected_sha`.
À la lecture, la suite doit exister à la tête et **tous ses jobs exécutables**
doivent être verts. Seuls des jobs de plomberie **nommés exactement**
(`lot-plan`, `lot-plan-retry`, `lot-pending`, `lot-status` et leurs jobs
appelés) et les exceptions **de la suite concernée** sont écartés :
`changed-paths` dans backend-suite, `github-bridge` dans compute, la matrice
`Desktop suite (...)` dans desktop-suite. Un job de test nommé `lot-*` ou
`Desktop suite (...)` hors de sa suite reste donc requis, comme tout job
nouveau — sans nouvelle relecture du contrat. Une liste due vide refuse : un
paquet documentation seule n'est pas une preuve de train. Le contrat est livré
pour le mode ombre seulement : aucune configuration d'hôte ni bascule n'est
faite par ce lot. Prérequis d'hôte : `bwrap` (N3 de #1666) et `node` (le
sélecteur) — sans eux, la preuve des parts est refusée, jamais remplacée par
une exécution non isolée.

## Retour arrière

Repasser `mode` à `shadow` (ou retirer la clé `train`) et redémarrer
l'exécuteur. Un passage actif en cours de CI conserve son manifeste et sa
réservation jusqu'à sa conclusion terminale : il ne fusionne pas, mais il ne
libère pas non plus la cible avant d'avoir lu son résultat. Le bail de service
expire seul (15 min) si l'exécuteur est tué.
