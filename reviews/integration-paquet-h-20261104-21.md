# Paquet integration/paquet-h-20261104-21 — composition (§12), sur main `ae5f4d55f065`

Base : `main` **`ae5f4d55f06565c52e95736ea72b9e5e57d9f9b4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-21`, cible `main`.
Porteur : **#5788**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5755 | `ffd29789340f` |
| #5784 | `6348e9efee1f` |
| #5785 | `a0d0bfa1c64c` |
| #5777 | `4896e399083b` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5755 | `ffd29789340f` | `692d2683b315` | composition |
| 2 | #5784 | `6348e9efee1f` | `08bd7607158a` | composition |
| 3 | #5785 | `a0d0bfa1c64c` | `52773b725540` | composition |
| 4 | #5777 | `4896e399083b` | `67337fd7c213` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
