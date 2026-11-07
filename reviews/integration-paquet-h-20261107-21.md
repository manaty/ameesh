# Paquet integration/paquet-h-20261107-21 — composition (§12), sur main `fbf61ba6c46a`

Base : `main` **`fbf61ba6c46a5da6710648bb13e872ba5749b821`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-21`, cible `main`.
Porteur : **#6208**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6142 | `18b9ad73e99e` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6142 | `18b9ad73e99e` | `a0fd6c193ad8` | composition |

## Écartés

| lot | motif |
|---|---|
| #6180 | tests par unité rouges (tentative 1/3, 17:10Z) : tests.test_l161f_plan.MapTest.test_always_list_sorted_and_known — la liste toujours.txt figée par le test ne contient pas les 4 contrats ajoutés par #6180 (test_contract_registry_ameesh…) — --exclude, recomposition sans lui |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 48.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
