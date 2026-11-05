# Paquet integration/paquet-h-20261105-03 — composition (§12), sur main `226234764052`

Base : `main` **`2262347640523b24bd4f40f7a22a5d1d8283bef6`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261105-03`, cible `main`.
Porteur : **#5855**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5849 | `dbeadb9db8c1` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5849 | `dbeadb9db8c1` | `832b942347e6` | composition |

## Écartés

| lot | motif |
|---|---|
| #5826 | conflit, coupure suivante — fichiers : src/ameesh/runner.py |

Liste calculée : 2 lot(s) prêt(s), 1 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 56.7 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
