# Paquet integration/paquet-h-20261103-10 — composition (§12), sur main `a159edee10e5`

Base : `main` **`a159edee10e502fdccadc812868f96734be4c489`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-10`, cible `main`.
Porteur : **#5593**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5587 | `9e46b25d2bae` |
| #5584 | `10e233afa03b` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5587 | `9e46b25d2bae` | `a723aefe947d` | composition |
| 2 | #5584 | `10e233afa03b` | `45cf06898c20` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 46.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
