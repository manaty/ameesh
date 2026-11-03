# Paquet integration/paquet-h-20261103-03 — composition (§12), sur main `f5eee12d6008`

Base : `main` **`f5eee12d6008a0113e34dc37146af7466c6e44d1`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-03`, cible `main`.
Porteur : **#5554**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (2 unités, 2 lots)

| lot | source composée |
|---|---|
| #5551 | `bc9fcd40bec2` |
| #5510 | `63ad771ab83b` |

## Composition (2 unités, 2 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5551 | `bc9fcd40bec2` | `d398a12b046c` | composition |
| 2 | #5510 | `63ad771ab83b` | `af834184d38e` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 2 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
