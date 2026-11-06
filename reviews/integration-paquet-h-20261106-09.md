# Paquet integration/paquet-h-20261106-09 — composition (§12), sur main `d40d55908aa3`

Base : `main` **`d40d55908aa364fcb279ffb6307bc1128029c511`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261106-09`, cible `main`.
Porteur : **#6027**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6024 | `a9eb2f3b37fd` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6024 | `a9eb2f3b37fd` | `3c792e2f9736` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 6 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 39.8 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
