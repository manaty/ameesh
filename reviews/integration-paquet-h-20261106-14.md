# Paquet integration/paquet-h-20261106-14 — composition (§12), sur main `73fe7912b292`

Base : `main` **`73fe7912b29297ef34a5f876afd728ff8b71ec99`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-14`, cible `main`.
Porteur : **#6050**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6020 | `c8f5c022f776` |
| #6040 | `5420d4d87faf` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6020 | `c8f5c022f776` | `d9ea248c4a7b` | composition |
| 2 | #6040 | `5420d4d87faf` | `06dc48c370ef` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
