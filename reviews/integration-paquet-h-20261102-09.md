# Paquet integration/paquet-h-20261102-09 — composition (§12), sur main `fcb1a0b2bb30`

Base : `main` **`fcb1a0b2bb3024c7026b9c66bdd2191672ea9980`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-09`, cible `main`.
Porteur : **#5473**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (6 unités, 6 lots)

| lot | source composée |
|---|---|
| #5424 | `bbfefe92aab5` |
| #5450 | `70b9a141d4a3` |
| #5466 | `c5d82e002e72` |
| #5475 | `9d758c1fbbb8` |
| #5444 | `a863f041bfe7` |
| #5470 | `7b1ac305ab54` |

## Composition (6 unités, 6 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5424 | `bbfefe92aab5` | `2784e033dcb1` | composition |
| 2 | #5450 | `70b9a141d4a3` | `1380a6aa7841` | composition |
| 3 | #5466 | `c5d82e002e72` | `5d55276cf83c` | composition |
| 4 | #5475 | `9d758c1fbbb8` | `8b3401f60b80` | composition |
| 5 | #5444 | `a863f041bfe7` | `d82190edf44e` | composition |
| 6 | #5470 | `7b1ac305ab54` | `c4c6f7747bc8` | composition |

## Écartés

| lot | motif |
|---|---|
| #5411 | exclu par le pilote — --exclude, recomposition sans lui |
| #5427 | exclu par le pilote — --exclude, recomposition sans lui |

Liste calculée : 6 lot(s) prêt(s), 2 en attente, 2 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 25.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
