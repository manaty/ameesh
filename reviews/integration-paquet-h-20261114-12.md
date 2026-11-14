# Paquet integration/paquet-h-20261114-12 — composition (§12), sur main `c13ebb696629`

Base : `main` **`c13ebb696629eb8c5b2aea720dbeb64f619b6690`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261114-12`, cible `main`.
Porteur : **#7382**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7350 | `e9465145fa1d` |
| #7364 | `4ecc7629eec8` |
| #7381 | `7b9fd44b5529` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7350 | `e9465145fa1d` | `957748a74376` | composition |
| 2 | #7364 | `4ecc7629eec8` | `2615c59ddd4c` | composition |
| 3 | #7381 | `7b9fd44b5529` | `b0230affea16` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 53.1 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
