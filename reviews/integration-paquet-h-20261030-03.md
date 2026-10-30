# Paquet integration/paquet-h-20261030-03 — composition (§12), sur main `57272e1e7417`

Base : `main` **`57272e1e7417f60aad6c9743ef893d69ebc67906`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261030-03`, cible `main`.
Porteur : **#4761**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4667 | `947335f7ab39` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4667 | `947335f7ab39` | `e80b80eb9994` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
