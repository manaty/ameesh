# Paquet integration/paquet-h-20261031-06 — composition (§12), sur main `f8d3c3740c6a`

Base : `main` **`f8d3c3740c6a9c510d9c28acbbfed5eab5e98133`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-06`, cible `main`.
Porteur : **#5071**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #4975 | `3874d447e641` |
| #5063 | `f6289dfc4684` |
| #4413 | `570f4bf0f2b2` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4975 | `3874d447e641` | `67d772063ceb` | composition |
| 2 | #5063 | `f6289dfc4684` | `cb6bb90b3199` | composition |
| 3 | #4413 | `570f4bf0f2b2` | `ef6e4a27b18b` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.4 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
