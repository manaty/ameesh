# Paquet integration/paquet-h-20261107-12 — composition (§12), sur main `c8a54b4578b3`

Base : `main` **`c8a54b4578b3589c9c8848885da8b16da4d21fb9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-12`, cible `main`.
Porteur : **#6146**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6090 | `347701b1cf75` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6090 | `347701b1cf75` | `c73d0d2a965f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 57.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
