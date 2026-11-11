# Paquet integration/paquet-h-20261111-01 — composition (§12), sur main `619e635be77b`

Base : `main` **`619e635be77b0bc3749c889c46e2663ee58897e9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261111-01`, cible `main`.
Porteur : **#6793**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #6786 | `d29e1d058cc5` |
| #6785 | `9a0321f80714` |
| #6671 | `b88abfdec20f` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6786 | `d29e1d058cc5` | `1e9ad8ab09bc` | composition |
| 2 | #6785 | `9a0321f80714` | `0a45e6770400` | composition |
| 3 | #6671 | `b88abfdec20f` | `d2ed21cc0f51` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 55.8 s.
- Types clients : types clients : inchangés.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Contrats du plan commun : passe bornée sur cet arbre après inclusion et avant poussée ; rapport réutilisé par les tests locaux, résultat à la PR et au porteur (#6182).
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
