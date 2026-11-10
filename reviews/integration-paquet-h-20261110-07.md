# Paquet integration/paquet-h-20261110-07 — composition (§12), sur main `7e1c5fe28e3d`

Base : `main` **`7e1c5fe28e3dc3bfb68101591e03baaa7ff9c8da`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-07`, cible `main`.
Porteur : **#6681**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6662 | `29babe41eddb` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6662 | `29babe41eddb` | `53eecdf3b1b4` | composition |

## Écartés

| lot | motif |
|---|---|
| #6643 | préflight N-1 (vm.sh) rouge (run 42805609436) : après retour arrière vers v1.47.1, role timers auditeur-court out of date ; #6643 change l'ExecStart de l'unité auditeur-court (role_timers.py) ; #6662 ne touche ni role_timers ni deploy — --exclude, recomposition sans lui |
| #6649 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6660 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6661 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6664 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6676 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6684 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6626 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 3 en attente, 8 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 68.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
