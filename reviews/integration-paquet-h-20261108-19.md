# Paquet integration/paquet-h-20261108-19 — composition (§12), sur main `8e46f78acd9b`

Base : `main` **`8e46f78acd9b2d988898834a5acb613274e95876`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-19`, cible `main`.
Porteur : **#6371**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6343 | `7d548dc35656` |
| #6364 | `86faacd01a70` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6343 | `7d548dc35656` | `cefd5bd1a291` | composition |
| 2 | #6364 | `86faacd01a70` | `08ce6175dda0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 60.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
