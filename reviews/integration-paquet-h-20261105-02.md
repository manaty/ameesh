# Paquet integration/paquet-h-20261105-02 — composition (§12), sur main `e31b3bb67beb`

Base : `main` **`e31b3bb67bebfdc69b958fd5b380e42d13efadea`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-02`, cible `main`.
Porteur : **#5828**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5806 | `6a186bfaea03` |
| #5778 | `295299cbf85b` |
| #5799 | `f433a84244a2` |
| #5793 | `24db1a035dfd` |
| #5804 | `7365dbb6e78f` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5806 | `6a186bfaea03` | `a3ec45e1fe52` | composition |
| 2 | #5778 | `295299cbf85b` | `9259657c7389` | composition |
| 3 | #5799 | `f433a84244a2` | `32e6d99a8908` | composition |
| 4 | #5793 | `24db1a035dfd` | `f5f332c0c4f4` | composition |
| 5 | #5804 | `7365dbb6e78f` | `6078bb8ce6f0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 37.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
