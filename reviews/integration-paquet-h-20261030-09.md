# Paquet integration/paquet-h-20261030-09 — composition (§12), sur main `6a9e96d53a9d`

Base : `main` **`6a9e96d53a9d03b326380290e79fca28f4e75a9d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-09`, cible `main`.
Porteur : **#4826**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #4806 | `58979aa13283` |
| #4810 | `d147257cfa66` |
| #4814 | `eb39f05151c6` |
| #4759 | `6f44b8389634` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4806 | `58979aa13283` | `7e059e4fe232` | composition |
| 2 | #4810 | `d147257cfa66` | `2561234aab49` | composition |
| 3 | #4814 | `eb39f05151c6` | `0c761ad5d215` | composition |
| 4 | #4759 | `6f44b8389634` | `2a6b67d2c90c` | composition |

## Écartés

| lot | motif |
|---|---|
| #4791 | rouge test_3301_prior_freeze_unknown en local dès 89d8e2f1b, vert sur main (mesh-design) — --exclude, recomposition sans lui |
| #4779 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4812 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4819 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4786 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #4830 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 9 lot(s) prêt(s), 8 en attente, 6 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
