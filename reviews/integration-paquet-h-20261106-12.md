# Paquet integration/paquet-h-20261106-12 — composition (§12), sur main `6d3c5162f916`

Base : `main` **`6d3c5162f916d120d748619ace8153168437bb93`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-12`, cible `main`.
Porteur : **#6035**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6005 | `3842bf4bdda8` |
| #6031 | `1c79ac51e87c` |
| #6034 | `3875872d9eb9` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6005 | `3842bf4bdda8` | `d1fafdcebc3c` | composition |
| 2 | #6031 | `1c79ac51e87c` | `8148f073507d` | composition |
| 3 | #6034 | `3875872d9eb9` | `21a9845ebb48` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
