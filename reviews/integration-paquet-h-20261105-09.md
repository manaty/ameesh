# Paquet integration/paquet-h-20261105-09 — composition (§12), sur main `174b1af9cde7`

Base : `main` **`174b1af9cde75d17485abad61fe02dba3f0b118b`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-09`, cible `main`.
Porteur : **#5888**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5880 | `7c893cbd6c0e` |
| #5826 | `4f5fecc3f3f0` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5880 | `7c893cbd6c0e` | `b5ac8b20fc91` | composition |
| 2 | #5826 | `4f5fecc3f3f0` | `b3ce702597ad` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 54.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
