# Paquet integration/paquet-h-20261106-01 — composition (§12), sur main `87ecf72c03ba`

Base : `main` **`87ecf72c03ba876d4fac17afab725e2429de07ef`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-01`, cible `main`.
Porteur : **#5994**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (4 unités, 4 lots)

| lot | source composée |
|---|---|
| #5967 | `6abe41153ab3` |
| #5979 | `a8be414a2d23` |
| #5985 | `a6d0ce1ad33f` |
| #5989 | `63f1efef7890` |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5967 | `6abe41153ab3` | `3e5a08f290c5` | composition |
| 2 | #5979 | `a8be414a2d23` | `341b0799deab` | composition |
| 3 | #5985 | `a6d0ce1ad33f` | `50fdb6681f53` | composition |
| 4 | #5989 | `63f1efef7890` | `e22136260192` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 4 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 40.6 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
