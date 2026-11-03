# Paquet integration/paquet-h-20261103-08 — composition (§12), sur main `ec50d89ee091`

Base : `main` **`ec50d89ee0916a07c7a4da09afc275e05973cb0e`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261103-08`, cible `main`.
Porteur : **#5572**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5562 | `03ce358a9b4e` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5562 | `03ce358a9b4e` | `ec54726aaaa0` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 3 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 45.9 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
