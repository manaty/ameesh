# Paquet integration/paquet-h-20261103-06 — composition (§12), sur main `a75053bc1c60`

Base : `main` **`a75053bc1c606ef5bb8c386114d4a80364bd7a28`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-06`, cible `main`.
Porteur : **#5569**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (3 unités, 3 lots)

| lot | source composée |
|---|---|
| #5556 | `35df97bae181` |
| #5561 | `289fe8b5deda` |
| #5563 | `e159e98c25cb` |

## Composition (3 unités, 3 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5556 | `35df97bae181` | `7ebcaace47d7` | composition |
| 2 | #5561 | `289fe8b5deda` | `2833854881c0` | composition |
| 3 | #5563 | `e159e98c25cb` | `d1a7553f5760` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 3 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 42.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
