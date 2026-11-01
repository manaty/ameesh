# Paquet integration/paquet-h-20261101-07 — composition (§12), sur main `538c0c21acbf`

Base : `main` **`538c0c21acbf81d4ba9b8e63c89569e8ac5836f9`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-07`, cible `main`.
Porteur : **#5237**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5207 | `36b10e48c419` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5207 | `36b10e48c419` | `e5ad5a0118ec` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 27.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
