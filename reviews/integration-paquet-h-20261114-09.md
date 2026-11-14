# Paquet integration/paquet-h-20261114-09 — composition (§12), sur main `85b0613da54b`

Base : `main` **`85b0613da54b5483891c84e2a4778707a06b3714`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-09`, cible `main`.
Porteur : **#7351**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #7308 | `80ea95b3d522` |
| #7339 | `e0612103182a` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7308 | `80ea95b3d522` | `55d87ed79136` | composition |
| 2 | #7339 | `e0612103182a` | `858eb92c6b04` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 61.3 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
