# Paquet integration/paquet-h-20261108-11 — composition (§12), sur main `97eb323299c4`

Base : `main` **`97eb323299c433f1d34a0851f82d032009730fa9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-11`, cible `main`.
Porteur : **#6303**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6291 | `0e81b26dbdc5` |
| #6296 | `9a12dd6e0a5c` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6291 | `0e81b26dbdc5` | `63ae014f1b7d` | composition |
| 2 | #6296 | `9a12dd6e0a5c` | `de71ab9b9385` | composition |

## Écartés

| lot | motif |
|---|---|
| #6298 | conflit, coupure suivante — fichiers : tests/test_contract_registry_ameesh.py |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
