# Paquet integration/paquet-h-20261104-05 — composition (§12), sur main `ba263e3b0102`

Base : `main` **`ba263e3b0102b6ea1ce6bf054025416557cf3b20`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261104-05`, cible `main`.
Porteur : **#5725**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #5710 | `1cbd6f24e6a4` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #5710 | `1cbd6f24e6a4` | `3d28bb10ab8d` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 44.5 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
