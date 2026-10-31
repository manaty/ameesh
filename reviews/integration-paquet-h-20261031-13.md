# Paquet integration/paquet-h-20261031-13 — composition (§12), sur main `f96bdd90a59a`

Base : `main` **`f96bdd90a59a48c62369c9415551f2fede912b1f`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-13`, cible `main`.
Porteur : **#5106**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (5 unités, 5 lots)

| lot | source composée |
|---|---|
| #5038 | `fc4b1c71b54d` |
| #5094 | `e7fe08ed6b8f` |
| #5057 | `12c1cffd7ba7` |
| #5097 | `e48d3bfb7fbc` |
| #5099 | `ca5555e444dd` |

## Composition (5 unités, 5 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5038 | `fc4b1c71b54d` | `71cfb5a6d76f` | composition |
| 2 | #5094 | `e7fe08ed6b8f` | `7ddf88fa66c0` | composition |
| 3 | #5057 | `12c1cffd7ba7` | `48a6216a6640` | composition |
| 4 | #5097 | `e48d3bfb7fbc` | `f8fb2b9fb441` | composition |
| 5 | #5099 | `ca5555e444dd` | `a261d7aab34f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 5 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 28.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
