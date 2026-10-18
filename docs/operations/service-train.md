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
   trop, paquet et base qui ont servi.

Rien n'est réservé, poussé, dispatché ni fusionné : aucun `merge_locks` de
cible, aucune branche `integration/**`, aucune CI. C'est le mode qui doit
tourner sur au moins deux paquets horaires réels avant la bascule.

Lecture :

```sh
jq '.plans[-2:]' "$AMEESH_STATE"/trains/shadow/*.json
jq '.comparisons[] | {packet: .packet.stamp, missing, extra, explanations}' \
  "$AMEESH_STATE"/trains/shadow/*.json
```

Les comparaisons sont conservées (24 plans, 48 comparaisons). Un plan dont le
paquet n'a pas encore atterri n'apparaît pas dans `comparisons`.

### `active` — la bascule

Le passage réel du moteur #1467 : il compose la tête du paquet, publie la
branche immuable `integration/train-*`, attend l'unique CI complète du paquet
(`paquet.yml` → `workflow_call` de `tests.yml`) et fusionne au vert. Le
composeur humain devient le repli : en cas de rouge, le train s'arrête, alerte
et n'enchaîne pas ; un humain reprend la main.

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

`train_contracts.ameesh()` épingle les blobs de `.github/workflows/paquet.yml`,
`.github/workflows/tests.yml` et des planificateurs (`test-plan.py`,
`test-parts.py`, `ci-flow.py`, `map.py`). La suite du paquet est la matrice
complète 8 psycopg + 16 psql, le job `plateforme macOS` et l'agrégat `tests`,
lus sur l'événement `workflow_call` de la tête poussée. Une modification d'un
de ces fichiers exige un nouveau contrat audité : le témoin
`tests/test_2446_train_contract.py` compare les blobs au commit.

## Retour arrière

Repasser `mode` à `shadow` (ou retirer la clé `train`) et redémarrer
l'exécuteur. Un passage actif en cours de CI conserve son manifeste et sa
réservation jusqu'à sa conclusion terminale : il ne fusionne pas, mais il ne
libère pas non plus la cible avant d'avoir lu son résultat. Le bail de service
expire seul (15 min) si l'exécuteur est tué.
