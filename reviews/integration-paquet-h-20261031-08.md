# Paquet integration/paquet-h-20261031-08 — composition (§12), sur main `125d3677eb8a`

Base : `main` **`125d3677eb8a0387d53e29188c323ab75520016a`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261031-08`, cible `main`.
Porteur : **#5081**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5038 | `fe303ddf5087` |
| #5077 | `2632aa053f0d` |
| #5080 | `2d3f0e04baaa` |
| #4414 | `d8737629ed8b` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5038 | `fe303ddf5087` | `095db6249090` | composition |
| 2 | #5077 | `2632aa053f0d` | `ba673e8342ce` | composition |
| 3 | #5080 | `2d3f0e04baaa` | `124dea93a703` | composition |
| 4 | #4414 | `d8737629ed8b` | `3cb6e492505d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 0 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
