# Paquet integration/paquet-h-20261031-05 — composition (§12), sur main `fc4a185e2ed4`

Base : `main` **`fc4a185e2ed417288bb2444ecdffb4374ccbf8f7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-05`, cible `main`.
Porteur : **#5068**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5015 | `f3ead39c494a` |
| #5029 | `b43e3a7fe227` |
| #5037 | `73e014dff191` |
| #5045 | `2791ea04bbfb` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5015 | `f3ead39c494a` | `cc3988c18866` | composition |
| 2 | #5029 | `b43e3a7fe227` | `11741a6e3726` | composition |
| 3 | #5037 | `73e014dff191` | `db0cd6ad3d5c` | composition |
| 4 | #5045 | `2791ea04bbfb` | `c7187a101d90` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
