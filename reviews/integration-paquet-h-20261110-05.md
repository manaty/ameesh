# Paquet integration/paquet-h-20261110-05 — composition (§12), sur main `988cbad42b29`

Base : `main` **`988cbad42b297bdcfe4791a7144c37478d137764`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-05`, cible `main`.
Porteur : **#6647**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6636 | `4e36c68e019a` |
| #6640 | `9c44275a45c5` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6636 | `4e36c68e019a` | `a429d5792bed` | composition |
| 2 | #6640 | `9c44275a45c5` | `8afcdb73be50` | composition |

## Écartés

| lot | motif |
|---|---|
| #6643 | rouge déterministe reproduit en local (CI run 42763566834, psycopg 8/8) : tests.test_l84_serve.OpenApiTest.test_committed_document_is_the_generated_one ; #6643 ajoute le type d'événement auditor.pass sans régénérer docs/api/openapi-v1.json (vert sur la composition 31fdf8935 sans #6643, rouge sur e5b60f4dc avec) — --exclude, recomposition sans lui |
| #6646 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6641 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #6642 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 5 lot(s) prêt(s), 7 en attente, 4 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 76.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
