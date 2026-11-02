# Paquet integration/paquet-h-20261102-12 — composition (§12), sur main `71b70dd5d000`

Base : `main` **`71b70dd5d00012c64fcc3e526fc2da906c0a1488`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-12`, cible `main`.
Porteur : **#5501**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5494 | `c8c1f60b4794` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5494 | `c8c1f60b4794` | `6e9006b47a24` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
