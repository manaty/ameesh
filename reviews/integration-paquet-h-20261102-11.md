# Paquet integration/paquet-h-20261102-11 — composition (§12), sur main `330e5a6fd0ed`

Base : `main` **`330e5a6fd0edb1c3de2f58fbc42cf73a02cd2a2b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-11`, cible `main`.
Porteur : **#5489**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5478 | `10c629bbc89c` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5478 | `10c629bbc89c` | `f2c5e26332fc` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
