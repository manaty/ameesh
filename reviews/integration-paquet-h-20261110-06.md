# Paquet integration/paquet-h-20261110-06 — composition (§12), sur main `d5e667d13f95`

Base : `main` **`d5e667d13f954f831406b799239e1b61091d56a9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-06`, cible `main`.
Porteur : **#6658**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (9 unités, 9 lots)

| lot | source composée |
|---|---|
| #6646 | `219a427cecc6` |
| #6638 | `82e07e58b634` |
| #6641 | `9ac8f86d1bd6` |
| #6642 | `54fdeb8f1470` |
| #6650 | `4c8b78676a8f` |
| #6377 | `5f6f0a073463` |
| #6536 | `60215b3aba74` |
| #6576 | `f29e4bf5b719` |
| #6602 | `6c7836b25081` |

## Composition (9 unités, 9 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6646 | `219a427cecc6` | `88f5a8e83333` | composition |
| 2 | #6638 | `82e07e58b634` | `25214bbb409b` | composition |
| 3 | #6641 | `9ac8f86d1bd6` | `2bcb6fb7c2e1` | composition |
| 4 | #6642 | `54fdeb8f1470` | `a50489bdf0f9` | composition |
| 5 | #6650 | `4c8b78676a8f` | `2af596dcc9b8` | composition |
| 6 | #6377 | `5f6f0a073463` | `a05f967a39f6` | composition |
| 7 | #6536 | `60215b3aba74` | `9695133f1890` | composition |
| 8 | #6576 | `f29e4bf5b719` | `ab57b4b5b69c` | composition |
| 9 | #6602 | `6c7836b25081` | `67ffa20bee75` | composition |

## Écartés

| lot | motif |
|---|---|
| #6643 | rouge déterministe reproduit en local (CI run 42774927665, job plan, étape « types clients à jour ») : ui/src/api/schema.d.ts périmé ; #6643 régénère docs/api/openapi-v1.json (auditor.pass) sans régénérer les types du client (node ui/scripts/gen-api.mjs) ; à jour sur a6d14b28d sans #6643, périmé sur 6a9f55d01 avec — --exclude, recomposition sans lui |
| #6649 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 10 lot(s) prêt(s), 3 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 59.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
