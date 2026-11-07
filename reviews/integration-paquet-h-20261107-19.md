# Paquet integration/paquet-h-20261107-19 — composition (§12), sur main `4838ba960607`

Base : `main` **`4838ba960607008dc5d7e24ea76b71e8b90ba688`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-19`, cible `main`.
Porteur : **#6196**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #6179 | `33f28fc02156` |
| #6106 | `16ffb5f8133e` |
| #6145 | `ed589226522f` |
| #6163 | `103ead676467` |
| #6189 | `2bc3826d22f9` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6179 | `33f28fc02156` | `923c9744dac2` | composition |
| 2 | #6106 | `16ffb5f8133e` | `db6a74a26aa4` | composition |
| 3 | #6145 | `ed589226522f` | `092ef1ef2152` | composition |
| 4 | #6163 | `103ead676467` | `857dd5a02165` | composition |
| 5 | #6189 | `2bc3826d22f9` | `ddafd8cad539` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
