# Paquet integration/paquet-h-20261107-02 — composition (§12), sur main `c5eeb0a4bfe3`

Base : `main` **`c5eeb0a4bfe3a509d11f12f08408758840c863cc`** (tête exacte d'`origin/main`). Branche `integration/paquet-h-20261107-02`, cible `main`.
Porteur : **#6087**. Composeur : `scripts/paquet-auto.py` (sans agent).

## Admissions (1 unités, 1 lots)

| lot | source composée |
|---|---|
| #6045 | `ce9b8aee3710` |

## Composition (1 unités, 1 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #6045 | `ce9b8aee3710` | `9c8bb3b445dc` | composition |

## Écartés

| lot | motif |
|---|---|
| — | aucun |

Liste calculée : 1 lot(s) prêt(s), 2 en attente, 0 en erreur (les erreurs sont écartées ci-dessus, jamais composées).

## Contrôles locaux

- `scripts/freeze-checks.py` : vert en 41.1 s.
- Tests locaux : joués par le composeur après la poussée, en parallèle de la CI complète (#4304) ; résultat à la PR, au porteur et au courrier du pilote.
- Modules directs par lot : lus au corps de la PR après les tests (#4887).

- `scripts/paquet-inclusion.py` : exécuté sur cette tête après le manifeste (résultat au courrier du pilote).
