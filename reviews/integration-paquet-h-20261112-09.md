# Paquet integration/paquet-h-20261112-09 — composition (§12), sur main `7b55551ff6ee`

Base : `main` **`7b55551ff6ee4352c6462f21b6f592dbfee4e4c7`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261112-09`, cible `main`.
Porteur : **#7126**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (10 unités, 10 lots)

| lot | source composée |
|---|---|
| #6990 | `e2c78c106a39` |
| #7004 | `505ae35af3aa` |
| #7092 | `e1529b7ee8ec` |
| #6861 | `2526396d08db` |
| #6893 | `c805611e8c24` |
| #6902 | `c7db59f6ce51` |
| #6934 | `1901d57d6735` |
| #7048 | `ea90161223bd` |
| #6856 | `bbf0a0252a3b` |
| #7116 | `453c0ad1f66f` |

## Composition (10 unités, 10 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6990 | `e2c78c106a39` | `75c97bfcd5f2` | composition |
| 2 | #7004 | `505ae35af3aa` | `765d044c30cf` | composition |
| 3 | #7092 | `e1529b7ee8ec` | `485dace8849e` | composition |
| 4 | #6861 | `2526396d08db` | `41b91a461d42` | composition |
| 5 | #6893 | `c805611e8c24` | `b713878ea1d9` | composition |
| 6 | #6902 | `c7db59f6ce51` | `b4015a58de35` | composition |
| 7 | #6934 | `1901d57d6735` | `be49a4c75d3d` | composition |
| 8 | #7048 | `ea90161223bd` | `52094c0bac7f` | composition |
| 9 | #6856 | `bbf0a0252a3b` | `3497510f1e7e` | composition |
| 10 | #7116 | `453c0ad1f66f` | `c8ccbf510876` | composition |

## Écartés

| lot | motif |
|---|---|
| #6961 | rouge CI réel (run 43302773205, psycopg 4/12) : tests.test_l242b_critical_accounts, 3 erreurs (profile None) ; reproduit seul sur c1afa239c2d3, vert sur main 7b55551ff6ee (mesh-design) — --exclude, recomposition sans lui |
| #7050 | rouge CI réel (run 43302773205, psql 12/16) : tests.test_migrations_additives — 0572_conversation_moves : trigger on a table not proven new ; reproduit seul sur 2da713461357, vert sur main (mesh-design) — --exclude, recomposition sans lui |

Liste calculée : 10 lot(s) prêt(s), 3 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 63.7 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
