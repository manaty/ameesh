# Paquet integration/paquet-h-20261029-10 — composition (§12), sur main `c253f129603c`

Base : `main` **`c253f129603cf2320ba517b5c2a94632ff949aab`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261029-10`, cible `main`.
Porteur : **#4654**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (12 unités, 12 lots)

| lot | source composée |
|---|---|
| #4610 | `b318a420ed7e` |
| #4616 | `1f324e0a332f` |
| #4384 | `454752d1f0d5` |
| #4396 | `44291681d250` |
| #4463 | `bf1fa94505c8` |
| #4508 | `f632f7295f99` |
| #4602 | `2b4732428e1e` |
| #4620 | `fa1d9c12eb1e` |
| #4552 | `7db121cc17c2` |
| #4579 | `c02224c07484` |
| #4593 | `fde0c71a2b85` |
| #4605 | `ca2d5f30498d` |

## Composition (12 unités, 12 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4610 | `b318a420ed7e` | `644e62d56c66` | composition |
| 2 | #4616 | `1f324e0a332f` | `edf6168ed010` | composition |
| 3 | #4384 | `454752d1f0d5` | `705c79652546` | composition |
| 4 | #4396 | `44291681d250` | `bbe9c321543c` | composition |
| 5 | #4463 | `bf1fa94505c8` | `7711c3114e11` | composition |
| 6 | #4508 | `f632f7295f99` | `6948eb1ed0b8` | composition |
| 7 | #4602 | `2b4732428e1e` | `8a11c81e9fe8` | composition |
| 8 | #4620 | `fa1d9c12eb1e` | `ab97e577fab5` | composition |
| 9 | #4552 | `7db121cc17c2` | `091265ef68a3` | composition |
| 10 | #4579 | `c02224c07484` | `3d8b033fa704` | composition |
| 11 | #4593 | `fde0c71a2b85` | `dff9229a08c2` | composition |
| 12 | #4605 | `ca2d5f30498d` | `0213c515384d` | composition |

## Écartés

| lot | motif |
|---|---|
| #4299 | run 40937191508 : test_705/test_2076 rouges sous pilote psql (« % » de 0516 lus comme paramètres) — --exclude, recomposition sans lui |
| #4609 | run 40937191508 : test_l162_tmpfs, cfg.mesh_backup_window absent des doublures — --exclude, recomposition sans lui |
| #4623 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4391 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4408 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4562 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4574 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 17 lot(s) prêt(s), 3 en attente, 7 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 32.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
