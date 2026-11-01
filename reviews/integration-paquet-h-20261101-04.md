# Paquet integration/paquet-h-20261101-04 — composition (§12), sur main `d7bc729c7ead`

Base : `main` **`d7bc729c7ead447790d1a3c266f2eee5c3d9559c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261101-04`, cible `main`.
Porteur : **#5224**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #5223 | `451b77bd17b4` |
| #5140 | `04a326d7badd` |
| #5191 | `130514fa0282` |
| #5212 | `2416209f44d7` |
| #5219 | `37ca50eb7c18` |
| #5221 | `b8b5c192d148` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5223 | `451b77bd17b4` | `b868daec7cf1` | composition |
| 2 | #5140 | `04a326d7badd` | `a185f2845239` | composition |
| 3 | #5191 | `130514fa0282` | `18c63e483fa8` | composition |
| 4 | #5212 | `2416209f44d7` | `5d747b04ec95` | composition |
| 5 | #5219 | `37ca50eb7c18` | `1320c992fded` | composition |
| 6 | #5221 | `b8b5c192d148` | `7977a3bfdb8c` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 6 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 22.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
