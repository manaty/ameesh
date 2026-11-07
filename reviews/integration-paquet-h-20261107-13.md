# Paquet integration/paquet-h-20261107-13 — composition (§12), sur main `962ce0e135c8`

Base : `main` **`962ce0e135c82e16b5e5e465587f7b018606cd71`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-13`, cible `main`.
Porteur : **#6150**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6103 | `3ba0d62f372e` |
| #6147 | `b638d888d509` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6103 | `3ba0d62f372e` | `537aee001f16` | composition |
| 2 | #6147 | `b638d888d509` | `8db01014000d` | composition |

## Écartés

| lot | motif |
|---|---|
| #6090 | CI rouge test_l107_executor_contract : work.by_unique_ref non classé au contrat L107 ; correctif de l'auteur — --exclude, recomposition sans lui |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 62.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
