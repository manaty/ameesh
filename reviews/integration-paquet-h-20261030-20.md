# Paquet integration/paquet-h-20261030-20 — composition (§12), sur main `7a93c404bb83`

Base : `main` **`7a93c404bb8348614488800a88078ec7aaf7f059`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-20`, cible `main`.
Porteur : **#5008**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #4398 | `6f193d998d55` |
| #4858 | `dfb547213f8d` |
| #4870 | `302e0a6b6bdc` |
| #4872 | `a3fb39fb2dd8` |
| #4965 | `71f0d09fa920` |
| #4881 | `175e0b95eea0` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4398 | `6f193d998d55` | `0cd3f1795d8b` | composition |
| 2 | #4858 | `dfb547213f8d` | `020b6c43a080` | composition |
| 3 | #4870 | `302e0a6b6bdc` | `c12be27a6640` | composition |
| 4 | #4872 | `a3fb39fb2dd8` | `12e0f8d252bc` | composition |
| 5 | #4965 | `71f0d09fa920` | `51a7eec87247` | composition |
| 6 | #4881 | `175e0b95eea0` | `4149b86847cb` | composition |

## Écartés

| lot | motif |
|---|---|
| #5005 | test_l234a_auditor_canon_guard rouge en CI et reproduit sur la tête du lot seule (9d64bd5884bc) : faux cfg lu par mechanics.configured dans candidates() — --exclude, recomposition sans lui |
| #4898 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4621 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 10 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 20.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
