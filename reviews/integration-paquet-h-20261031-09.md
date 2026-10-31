# Paquet integration/paquet-h-20261031-09 — composition (§12), sur main `2600437a9cb0`

Base : `main` **`2600437a9cb0c26a0aabc27a2821360a0500765a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-09`, cible `main`.
Porteur : **#5090**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5077 | `86445448ed49` |
| #5087 | `85271296bd02` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5077 | `86445448ed49` | `8b9a653b1a38` | composition |
| 2 | #5087 | `85271296bd02` | `d13790388140` | composition |

## Écartés

| lot | motif |
|---|---|
| #5038 | tests ciblés rouges (tests.test_l124_decisions.PlateformeTest.test_environnement_d_un_processus), attribués à son unité (#3801), retirés de la recomposition — test tests/test_l124_decisions.py ; covered src/ameesh/acp.py ; covered src/ameesh/chat.py |
| #5082 | tests ciblés rouges (tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue), attribués à son unité (#3801), retirés de la recomposition — test tests/test_cluster.py ; support tests/role_acl.py |

Liste calculée : 4 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 19.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
