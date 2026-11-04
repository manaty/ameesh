# Paquet integration/paquet-h-20261104-04 — composition (§12), sur main `d72b00b23124`

Base : `main` **`d72b00b231246ccf97ba33850f341a745fb0eb30`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-04`, cible `main`.
Porteur : **#5720**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5692 | `b2cb254cfde1` |
| #5695 | `fb5545a8505c` |
| #5711 | `a7973ad69c6f` |
| #5693 | `67a551e7a493` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5692 | `b2cb254cfde1` | `1e1b34b7f027` | composition |
| 2 | #5695 | `fb5545a8505c` | `480bec5b3da4` | composition |
| 3 | #5711 | `a7973ad69c6f` | `f5a8bf549416` | composition |
| 4 | #5693 | `67a551e7a493` | `dc308788ba60` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
