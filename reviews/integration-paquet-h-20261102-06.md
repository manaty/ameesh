# Paquet integration/paquet-h-20261102-06 — composition (§12), sur main `9bed94e8fd40`

Base : `main` **`9bed94e8fd4074a621f82d347209d46b52f95057`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261102-06`, cible `main`.
Porteur : **#5457**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5451 | `de737b588a66` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5451 | `de737b588a66` | `22d142bd4b02` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 5 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 31.2 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
