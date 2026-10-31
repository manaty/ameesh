# Paquet integration/paquet-h-20261031-18 — composition (§12), sur main `e7083472d542`

Base : `main` **`e7083472d542dec63755ddf85926e67f7d601cc6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-18`, cible `main`.
Porteur : **#5157**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5145 | `4dc6f8538602` |
| #5121 | `35e6a36816ba` |
| #5142 | `936a4174bdea` |
| #5146 | `9b5e3844e134` |
| #5155 | `eff53102e117` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5145 | `4dc6f8538602` | `28269c97c6ee` | composition |
| 2 | #5121 | `35e6a36816ba` | `2192f3ae9fd6` | composition |
| 3 | #5142 | `936a4174bdea` | `d4deb3c53cee` | composition |
| 4 | #5146 | `9b5e3844e134` | `2c02e2001676` | composition |
| 5 | #5155 | `eff53102e117` | `59bfaf66112b` | composition |

## Écartés

| lot | motif |
|---|---|
| #5083 | rouge ci-full 41234215336 : test_5077 appelle notify.main --once dans le processus de suite, donc child_environment.install() ; Popen retire alors AMEESH_DSN à tous les enfants suivants (repli 127.0.0.1:55432 refusé, 35 échecs psycopg 1/8) ; prouvé en local sur d729a1dcc55d. Correctif : neutraliser install() dans le test ou restaurer Popen.__init__ par addCleanup — --exclude, recomposition sans lui |
| #4972 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5041 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5101 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5134 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5148 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5051 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5136 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5159 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 13 lot(s) prêt(s), 9 en attente, 9 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 29.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
