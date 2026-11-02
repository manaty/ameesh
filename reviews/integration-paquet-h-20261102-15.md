# Paquet integration/paquet-h-20261102-15 — composition (§12), sur main `ad3275da314b`

Base : `main` **`ad3275da314bb4effddf2e0a1b382301bbbfd5b7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-15`, cible `main`.
Porteur : **#5515**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5506 | `ffa11ef377d6` |
| #5512 | `626dc2bf7b63` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5506 | `ffa11ef377d6` | `4b0d91abe1a3` | composition |
| 2 | #5512 | `626dc2bf7b63` | `cd61942bd402` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 8 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
