# Paquet integration/paquet-h-20261030-18 — composition (§12), sur main `1d14075c5730`

Base : `main` **`1d14075c5730d41b0f5644c6652db5229aa2879f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-18`, cible `main`.
Porteur : **#4997**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (7 unités, 7 lots)

| lot | source composée |
|---|---|
| #4804 | `061b47f22863` |
| #4963 | `2acf7fbe71ce` |
| #4986 | `b217420cbec8` |
| #4397 | `99cb896d38eb` |
| #4880 | `8d415aae1d4f` |
| #4889 | `1ba03a74ff8a` |
| #4412 | `bf0285c6ae1a` |

## Composition (7 unités, 7 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4804 | `061b47f22863` | `191510668afd` | composition |
| 2 | #4963 | `2acf7fbe71ce` | `a55f04233952` | composition |
| 3 | #4986 | `b217420cbec8` | `6355e97c0361` | composition |
| 4 | #4397 | `99cb896d38eb` | `8c07e4521ec6` | composition |
| 5 | #4880 | `8d415aae1d4f` | `188e7ceacd82` | composition |
| 6 | #4889 | `1ba03a74ff8a` | `c0ddfe6ca209` | composition |
| 7 | #4412 | `bf0285c6ae1a` | `14ffab9d6db0` | composition |

## Écartés

| lot | motif |
|---|---|
| #4975 | test_2442 ExecutorPassTest rouge sur la tête du lot seule : writer_tick lit self.db, faux runner sans db (freeze-checks, contrat de la porte #3687) — --exclude, recomposition sans lui |

Liste calculée : 7 lot(s) prêt(s), 7 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
