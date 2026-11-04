# Paquet integration/paquet-h-20261104-11 — composition (§12), sur main `6add86b55b07`

Base : `main` **`6add86b55b07dde09d2216087983fefbd5fd3b1b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-11`, cible `main`.
Porteur : **#5749**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5742 | `b4025bf75cc4` |
| #5741 | `b09fae91545b` |
| #5743 | `595ea83cfa16` |
| #5738 | `40f45ff951d3` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5742 | `b4025bf75cc4` | `295b025a22e0` | composition |
| 2 | #5741 | `b09fae91545b` | `0523479f0f78` | composition |
| 3 | #5743 | `595ea83cfa16` | `d892e7f43ba5` | composition |
| 4 | #5738 | `40f45ff951d3` | `b0e7ff44421b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 37.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
