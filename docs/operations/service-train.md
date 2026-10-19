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

## Contrat CI

`train_contracts.ameesh()` épingle les blobs de `.github/workflows/paquet.yml`
(le run appelant), `.github/workflows/tests.yml` (le workflow appelé) et des
planificateurs (`test-plan.py`, `test-parts.py`, `ci-flow.py`, `map.py`). La
preuve est le run `push` de `paquet.yml` à la tête exacte : il doit référencer
`tests.yml` au même SHA et porter les jobs appelés, préfixés par le job
appelant (`suite complète du paquet / plan`, `… / psycopg 4/8`, `… / tests`) :
la matrice complète 8 psycopg + 16 psql, le job `plateforme macOS` et l'agrégat
`tests`. Une PR ouverte laisse `suite` sauté : les jobs appelés sont absents et
le train ne s'en sert jamais. Une modification d'un de ces fichiers exige un
nouveau contrat audité : le témoin `tests/test_2446_train_contract.py` compare
les blobs au commit.

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
