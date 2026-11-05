# Paquet integration/paquet-h-20261105-25 — composition (§12), sur main `5a4476312bef`

Base : `main` **`5a4476312bef13cfb7cf45b54b65aa5fc29f00db`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-25`, cible `main`.
Porteur : **#5953**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5932 | `1255732b51f8` |
| #5950 | `9a17b1d6c6ca` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5932 | `1255732b51f8` | `e88a13974571` | composition |
| 2 | #5950 | `9a17b1d6c6ca` | `4dddf96b5158` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 47.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
