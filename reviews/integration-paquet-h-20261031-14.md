# Paquet integration/paquet-h-20261031-14 — composition (§12), sur main `13927ede8ceb`

Base : `main` **`13927ede8cebf3f557a5a5359d42bcd80d3c4aed`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-14`, cible `main`.
Porteur : **#5123**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5077 | `316984bba96b` |
| #5110 | `9ae223a40f59` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5077 | `316984bba96b` | `c9b822153067` | composition |
| 2 | #5110 | `9ae223a40f59` | `1176f2a303a9` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 29.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
