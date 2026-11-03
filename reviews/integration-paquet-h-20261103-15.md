# Paquet integration/paquet-h-20261103-15 — composition (§12), sur main `cb697d3d89f7`

Base : `main` **`cb697d3d89f70a05e0be13c765b9cb35124ddd76`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-15`, cible `main`.
Porteur : **#5651**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5585 | `86fa97e0306f` |
| #5629 | `9df5a6de6852` |
| #5607 | `762db89faa2d` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5585 | `86fa97e0306f` | `1c25abdfd5c4` | composition |
| 2 | #5629 | `9df5a6de6852` | `ce8b355a0e2d` | composition |
| 3 | #5607 | `762db89faa2d` | `7d1a0f5d00c0` | composition |

## Écartés

| lot | motif |
|---|---|
| #5635 | coupure suivante : tête regelée — composé 5f05edb41cb8, tête actuelle 369c726c4fa3 : pas de ci de paquet sur la nouvelle tête |
| #5649 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5652 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5655 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #5656 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 8 lot(s) prêt(s), 3 en attente, 5 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
