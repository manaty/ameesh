# Paquet integration/paquet-h-20261115-09 — composition (§12), sur main `b72b708b8992`

Base : `main` **`b72b708b899257a9fc87a9e9a88b6acb794ba86d`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261115-09`, cible `main`.
Porteur : **#7485**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #7476 | `afc61ed56cd1` |
| #7477 | `fbccf4b40e15` |
| #7475 | `0e54757b0f63` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #7476 | `afc61ed56cd1` | `0dedf7cbc4d8` | composition |
| 2 | #7477 | `fbccf4b40e15` | `a945c2a8b507` | composition |
| 3 | #7475 | `0e54757b0f63` | `59ae0b11c6e1` | composition |

## Écartés

| lot | motif |
|---|---|
| #7483 | rouge déterministe en ci-full (run 43703360719) : test_2179 ordre de l'aide racine et préflight N-1 test_5947 (unité carte modifiée), tous deux dus à #7483 ; retiré sans relire le reste (§11 bis) — --exclude, recomposition sans lui |

Liste calculée : 3 lot(s) prêt(s), 4 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.5 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
