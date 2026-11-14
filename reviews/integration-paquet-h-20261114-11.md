# Paquet integration/paquet-h-20261114-11 — composition (§12), sur main `4898a1cc44f9`

Base : `main` **`4898a1cc44f91e00c7077570c884e84034291f59`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-11`, cible `main`.
Porteur : **#7376**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7353 | `3a62feb6e7d1` |
| #7355 | `768c5cf583c8` |
| #7365 | `d4ff12e31fc8` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7353 | `3a62feb6e7d1` | `9d32d23da74e` | composition |
| 2 | #7355 | `768c5cf583c8` | `987a16ce7b06` | composition |
| 3 | #7365 | `d4ff12e31fc8` | `3bdec4a978e5` | composition |

## Écartés

| lot | motif |
|---|---|
| #7350 | tests ciblés rouges (tests.test_l87c_alert_ack.AcknowledgementTest.test_notify_without_thresholds_declares_the_perimeter_it_observed), situés au premier préfixe de l'empilement, retirés de la recomposition — préfixe base+u1…u1 (premier rouge à #7350) |
| #7364 | coupure suivante : tête regelée — composé c1f954c55a12, tête actuelle 4ecc7629eec8 : pas de ci de paquet sur la nouvelle tête |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.4 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
