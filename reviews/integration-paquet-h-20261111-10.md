# Paquet integration/paquet-h-20261111-10 — composition (§12), sur main `aa319f1d4dd8`

Base : `main` **`aa319f1d4dd819a95ccad3f5fe78fbaf8ec0bb4f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-10`, cible `main`.
Porteur : **#6855**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6832 | `db2edfefcc82` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6832 | `db2edfefcc82` | `a08a3beb1384` | composition |

## Écartés

| lot | motif |
|---|---|
| #6854 | coupure suivante : tête regelée — composé 1a00669c5e11, tête actuelle f466c6bc2bb1 : pas de ci de paquet sur la nouvelle tête |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.0 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
