# Paquet integration/paquet-h-20261105-04 — composition (§12), sur main `eef092db6a71`

Base : `main` **`eef092db6a7154e7687f0d6b40c90f643724652d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-04`, cible `main`.
Porteur : **#5868**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5853 | `477057c359f4` |
| #5854 | `8a0c3e748cea` |
| #5858 | `ab6abbd844c7` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5853 | `477057c359f4` | `7b64b3a2a29d` | composition |
| 2 | #5854 | `8a0c3e748cea` | `9fc2b8e05d81` | composition |
| 3 | #5858 | `ab6abbd844c7` | `a7d11e7ba22a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 50.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
