# Paquet integration/paquet-h-20261102-2102 — composition (§12), sur main `5386f1bd820c`

Base : `main` **`5386f1bd820c85097b7e0061b5aa8d15f47976f4`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-2102`, cible `main`.
Porteur : **#5544**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5542 | `4aa7f0422797` |
| #5537 | `d93ab5189c0b` |
| #5541 | `cf3199c2ad7d` |
| #5538 | `de3d2d029a93` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5542 | `4aa7f0422797` | `0cd291eb75d2` | composition |
| 2 | #5537 | `d93ab5189c0b` | `e7f38de2ec08` | composition |
| 3 | #5541 | `cf3199c2ad7d` | `25ef2133339c` | composition |
| 4 | #5538 | `de3d2d029a93` | `3d86a2ec46b2` | composition |

## Écartés

| lot | motif |
|---|---|
| #5503 | erreur paquet-candidats, jamais composé — cannot read the review gate: author provenance unknown for frozen commit(s): d81478a1e8a2 |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 1 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 43.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
