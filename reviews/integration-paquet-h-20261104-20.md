# Paquet integration/paquet-h-20261104-20 — composition (§12), sur main `ae5f4d55f065`

Base : `main` **`ae5f4d55f06565c52e95736ea72b9e5e57d9f9b4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-20`, cible `main`.
Porteur : **#5782**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5755 | `ffd29789340f` |
| #5777 | `4896e399083b` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5755 | `ffd29789340f` | `fd54a7014a60` | composition |
| 2 | #5777 | `4896e399083b` | `e2da63490480` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 36.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
