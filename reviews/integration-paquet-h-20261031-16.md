# Paquet integration/paquet-h-20261031-16 — composition (§12), sur main `f3d8b0e32a06`

Base : `main` **`f3d8b0e32a06aa5599b9cddb907cd34eff1a71bc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-16`, cible `main`.
Porteur : **#5131**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #4971 | `27fc5e55210d` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #4971 | `27fc5e55210d` | `9daacf305790` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 24.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
