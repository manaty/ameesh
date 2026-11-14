# Paquet integration/paquet-h-20261114-10 — composition (§12), sur main `8efbe9c435a3`

Base : `main` **`8efbe9c435a39ae7af51e01558b7bda701904138`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-10`, cible `main`.
Porteur : **#7359**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #7354 | `a83be4acb2fa` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7354 | `a83be4acb2fa` | `9a81998a4ecc` | composition |

## Écartés

| lot | motif |
|---|---|
| #7353 | coupure suivante : tête regelée — composé c6df9b98ca1a, tête actuelle 3a62feb6e7d1 : pas de ci de paquet sur la nouvelle tête |
| #7350 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |
| #7355 | coupure suivante : recomposition — hors de la composition conservée ; non admis, coupure suivante |

Liste calculée : 4 lot(s) prêt(s), 3 en attente, 3 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 62.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
