# Paquet integration/paquet-h-20261108-21 — composition (§12), sur main `01eb7f7d00ca`

Base : `main` **`01eb7f7d00caf1ff5e836c1bcbd6c52eae7e163b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-21`, cible `main`.
Porteur : **#6387**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6378 | `cb4a8f74c1a9` |
| #6339 | `1807ffccd8e1` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6378 | `cb4a8f74c1a9` | `bafa0df7315e` | composition |
| 2 | #6339 | `1807ffccd8e1` | `631c21c68bd8` | composition |

## Écartés

| lot | motif |
|---|---|
| #6365 | rouge déterministe tests.test_l107_executor_contract.TableTest.test_chaque_operation_existe_avec_sa_signature : #6365 (9cf4ac4d) ajoute evidence à une opération de stockage sans mettre à jour la table du contrat d'exécuteur ; rouge sur la tête de #6365 seule, vert sur la base 01eb7f7d — --exclude, recomposition sans lui |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 59.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
