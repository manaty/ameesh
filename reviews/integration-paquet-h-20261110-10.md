# Paquet integration/paquet-h-20261110-10 — composition (§12), sur main `cae88bcc31e0`

Base : `main` **`cae88bcc31e01dcbee04053ff5ef60ef7e27c40d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261110-10`, cible `main`.
Porteur : **#6726**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #6649 | `00b4ecba2e77` |
| #6670 | `7f56781a6e03` |
| #6676 | `f52d5b4b6dbf` |
| #6703 | `e70e7c69f92b` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6649 | `00b4ecba2e77` | `670b9aa1a59a` | composition |
| 2 | #6670 | `7f56781a6e03` | `139389e886ba` | composition |
| 3 | #6676 | `f52d5b4b6dbf` | `0a0abf4802b4` | composition |
| 4 | #6703 | `e70e7c69f92b` | `c179236849b7` | composition |

## Écartés

| lot | motif |
|---|---|
| #6670 | conflit, coupure suivante — fichiers : src/ameesh/model_evaluation_report.py |
| #6669 | coupure suivante : tête regelée — composé 8617ed88c52b, tête actuelle 28158ce33663 : pas de ci de paquet sur la nouvelle tête |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 52.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
