# Paquet integration/paquet-h-20261106-06 — composition (§12), sur main `21caaef1d385`

Base : `main` **`21caaef1d3854cf235570f9dfd7b638885b53e09`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-06`, cible `main`.
Porteur : **#6023**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6013 | `88350af4e1be` |
| #6022 | `a8833ae4f6d9` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6013 | `88350af4e1be` | `6df77e4ee565` | composition |
| 2 | #6022 | `a8833ae4f6d9` | `61cd2fd79aac` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
