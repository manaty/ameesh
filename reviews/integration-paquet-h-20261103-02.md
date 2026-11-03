# Paquet integration/paquet-h-20261103-02 — composition (§12), sur main `096d8ebd86f0`

Base : `main` **`096d8ebd86f01235c679aa3b736e5ddd080d983f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-02`, cible `main`.
Porteur : **#5550**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5548 | `1f15ad429b7a` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5548 | `1f15ad429b7a` | `3b0410493f8e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 37.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
