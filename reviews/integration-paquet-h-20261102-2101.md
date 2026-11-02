# Paquet integration/paquet-h-20261102-2101 — composition (§12), sur main `b9a026a02d7b`

Base : `main` **`b9a026a02d7b9349e96f61c5298ac544dd43f6fd`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-2101`, cible `main`.
Porteur : **#5539**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5481 | `f0bc0a90f0b6` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5481 | `f0bc0a90f0b6` | `f46f36810b46` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
