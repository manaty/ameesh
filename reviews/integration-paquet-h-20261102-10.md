# Paquet integration/paquet-h-20261102-10 — composition (§12), sur main `f22fd4c01bac`

Base : `main` **`f22fd4c01bacfe61b6649011ee0cdb71110a347e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-10`, cible `main`.
Porteur : **#5483**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5411 | `add56f0585a4` |
| #5427 | `c639c8ec1b54` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5411 | `add56f0585a4` | `169ffc89c4a8` | composition |
| 2 | #5427 | `c639c8ec1b54` | `133998699d7a` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 38.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
