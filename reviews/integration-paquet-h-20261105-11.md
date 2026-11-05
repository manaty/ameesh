# Paquet integration/paquet-h-20261105-11 — composition (§12), sur main `54e4d9853dfe`

Base : `main` **`54e4d9853dfeca2191798094c77a3eec64575097`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-11`, cible `main`.
Porteur : **#5894**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5891 | `6d4a8ba5bf75` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5891 | `6d4a8ba5bf75` | `bb0b9b28cc23` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
