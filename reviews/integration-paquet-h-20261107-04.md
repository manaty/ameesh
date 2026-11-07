# Paquet integration/paquet-h-20261107-04 — composition (§12), sur main `46fd8f8ac15d`

Base : `main` **`46fd8f8ac15d807a11efa8ac864722b42961530c`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-04`, cible `main`.
Porteur : **#6095**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #6039 | `fd46b1088b1e` |
| #6088 | `8afb01030cb9` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6039 | `fd46b1088b1e` | `5cebbdfb4422` | composition |
| 2 | #6088 | `8afb01030cb9` | `211cac50c6ee` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 4 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.3 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
