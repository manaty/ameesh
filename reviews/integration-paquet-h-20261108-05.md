# Paquet integration/paquet-h-20261108-05 — composition (§12), sur main `71ec56e3a8ae`

Base : `main` **`71ec56e3a8ae7aff85d6b9c5fa99d2cd8e4ba017`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261108-05`, cible `main`.
Porteur : **#6268**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6257 | `000aef65e3c9` |
| #6264 | `5ac6cbbcbafb` |
| #6238 | `ac7f4accb062` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6257 | `000aef65e3c9` | `b9bef922a8ea` | composition |
| 2 | #6264 | `5ac6cbbcbafb` | `18e961c83e4e` | composition |
| 3 | #6238 | `ac7f4accb062` | `dede5a6a576b` | composition |

## Écartés

| lot | motif |
|---|---|
| #6258 | rouge en CI complète seulement (vert 3/3 en local sur l'arbre du paquet) : tests.test_3277_tmp_over_quota.OverQuotaTest.test_former_key_drops_a_generated_plan_from_a_prevalidation_tree, test ajouté par le lot, dépendant de l'environnement ; retiré pour ne pas rendre main rouge en CI, canon §3 et §11 bis — --exclude, recomposition sans lui |

Liste calculée : 3 lot(s) prêt(s), 5 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
