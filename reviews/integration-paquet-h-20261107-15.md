# Paquet integration/paquet-h-20261107-15 — composition (§12), sur main `87411222037d`

Base : `main` **`87411222037d76454ffe5caef15458bcaaa04620`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-15`, cible `main`.
Porteur : **#6162**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6157 | `789073a51372` |
| #6153 | `ee378f48d646` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6157 | `789073a51372` | `d8db860859f1` | composition |
| 2 | #6153 | `ee378f48d646` | `cef1cc3c9d85` | composition |

## Écartés

| lot | motif |
|---|---|
| #6152 | coupure suivante : tête regelée — composé 15a6d27b68b1, tête actuelle 77015c9a2f8a : pas de ci de paquet sur la nouvelle tête |
| #6161 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 60.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
