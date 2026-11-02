# Paquet integration/paquet-h-20261102-07 — composition (§12), sur main `a67ee0381dce`

Base : `main` **`a67ee0381dcec48c40fbfb0ca37aa62f785fe474`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-07`, cible `main`.
Porteur : **#5462**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5347 | `7e7d161bea97` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5347 | `7e7d161bea97` | `c9e9664c46f7` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 8 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
