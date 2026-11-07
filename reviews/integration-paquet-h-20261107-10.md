# Paquet integration/paquet-h-20261107-10 — composition (§12), sur main `166d2f9eb86c`

Base : `main` **`166d2f9eb86cc7248d69a9ea38ba1a2c60c05ec8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-10`, cible `main`.
Porteur : **#6137**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6091 | `be96b8ab719e` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6091 | `be96b8ab719e` | `618fd4a2912f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 51.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
