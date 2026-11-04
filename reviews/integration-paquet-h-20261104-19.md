# Paquet integration/paquet-h-20261104-19 — composition (§12), sur main `fd8b91ee2e8d`

Base : `main` **`fd8b91ee2e8d9ff08cab979e7d1510417bec5d42`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-19`, cible `main`.
Porteur : **#5779**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5774 | `d7638eeaee28` |
| #5770 | `28ef5633b1e5` |
| #5771 | `13a91042b46f` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5774 | `d7638eeaee28` | `d3dffaba7249` | composition |
| 2 | #5770 | `28ef5633b1e5` | `5a04e2de6e74` | composition |
| 3 | #5771 | `13a91042b46f` | `c9d4ee0adfb4` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.0 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
