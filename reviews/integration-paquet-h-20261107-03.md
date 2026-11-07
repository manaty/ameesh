# Paquet integration/paquet-h-20261107-03 — composition (§12), sur main `fcc4db3fc458`

Base : `main` **`fcc4db3fc45884b46394ec0d5a84409a3c6dfbe8`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-03`, cible `main`.
Porteur : **#6094**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6080 | `3c69b7d3ad4a` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6080 | `3c69b7d3ad4a` | `f021043b440f` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
